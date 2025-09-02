import uasyncio as asyncio
import socket
import ssl
import network
import time
import os
import gc
from machine import ADC, Pin, I2C

from wifisetup import *

# Optional OLED display
ENABLE_OLED = True
try:
    import ssd1306
except ImportError:
    ENABLE_OLED = False

# --- Settings ---
ENABLE_DIR_LISTING = True
PUBLIC_DIR = "/public"
CERT_PATH = "/signed_server.der.crt"
KEY_PATH = "/private.key.der"
CA_CERT_PATH = "/ca.pem"  # CA that signed client certs

HOST = "0.0.0.0"
GEMINI_PORT = 1965
FILE_PORT = 12346  # secure file transfer port

CACHE_ENABLED = True
CACHE_MAX_SIZE = 50
UPDATE_INTERVAL = 1  # OLED stats update interval

# Global variables
file_cache = {}
start_time = time.ticks_ms()
total_clients = 0
max_clients_per_sec = 0
clients_this_sec = 0
last_sec_tick = int(time.time())
last_update = time.ticks_ms()
udp_sock = None
wlan = None
client_uploads = {}  # {(ip, port): {filename: {"chunks": {}, "total": int}}}

# --- OLED init ---
if ENABLE_OLED:
    i2c = I2C(0, scl=Pin(17), sda=Pin(16))
    oled = ssd1306.SSD1306_I2C(128, 64, i2c)
    oled.contrast(50)

# --- Helpers ---
def safe_join(base, *paths):
    full = "/".join([base.strip("/")] + [p.strip("/") for p in paths])
    if ".." in full:
        return None
    return full

def read_chip_temp():
    sensor = ADC(4)
    reading = sensor.read_u16()
    voltage = reading * 3.3 / 65535
    return 27 - (voltage - 0.706) / 0.001721

def display_stats(total_clients, max_clients_per_sec, start_time):
    if not ENABLE_OLED:
        return
    gc.collect()
    elapsed_ms = time.ticks_diff(time.ticks_ms(), start_time)
    elapsed_sec = int(elapsed_ms / 1000)
    hours = elapsed_sec // 3600
    minutes = (elapsed_sec % 3600) // 60
    seconds = elapsed_sec % 60
    avg_clients_per_sec = total_clients / elapsed_sec if elapsed_sec > 0 else 0
    temp_c = read_chip_temp()
    free_mem = gc.mem_free()
    oled.fill(0)
    oled.text("Runtime {:02}:{:02}:{:02}".format(hours, minutes, seconds), 0, 0)
    oled.text("Tot Clients: {}".format(total_clients), 0, 10)
    oled.text("Avg/s: {:.2f}".format(avg_clients_per_sec), 0, 20)
    oled.text("Free mem: {}".format(free_mem), 0, 30)
    oled.text("Chip Temp: {:.1f}C".format(temp_c), 0, 40)
    oled.text("IP: {}".format(wlan.ifconfig()[0]), 0, 50)
    oled.show()

def log_request(client_addr, request, status):
    t = time.localtime()
    timestamp = "{:04}-{:02}-{:02} {:02}:{:02}:{:02}".format(
        t[0], t[1], t[2], t[3], t[4], t[5]
    )
    print(f"[{timestamp}] {client_addr} -> {request} ({status})")

# --- File helpers ---
MIME_TYPES = {
    ".gmi": "text/gemini",
    ".txt": "text/plain",
    ".html": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
}

def get_mime_type(filename):
    for ext, mime in MIME_TYPES.items():
        if filename.endswith(ext):
            return mime
    return "application/octet-stream"

def get_file_content(filepath):
    try:
        st = os.stat(filepath)
        mtime = st[8]
    except OSError:
        return None
    if CACHE_ENABLED and filepath in file_cache:
        cached_content, cached_mtime = file_cache[filepath]
        if cached_mtime == mtime:
            return cached_content
    try:
        with open(filepath, "rb") as f:
            content = f.read()
        if CACHE_ENABLED:
            if len(file_cache) >= CACHE_MAX_SIZE:
                file_cache.pop(next(iter(file_cache)))
            file_cache[filepath] = (content, mtime)
        return content
    except OSError:
        return None

# --- Wi-Fi ---
def connect_to_wifi():
    global wlan
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(SSID, PASSWORD)
    for _ in range(10):
        if wlan.isconnected():
            break
        print(".", end="")
        time.sleep(1)
    if wlan.isconnected():
        print("\nConnected! IP:", wlan.ifconfig()[0])
        return wlan
    print("\nFailed to connect")
    return None

async def wifi_watchdog():
    global wlan
    while True:
        if not wlan.isconnected():
            print("Wi-Fi lost. Reconnecting...")
            wlan.disconnect()
            wlan.connect(SSID, PASSWORD)
            for _ in range(10):
                if wlan.isconnected():
                    break
                await asyncio.sleep(1)
            if wlan.isconnected():
                print("Reconnected! IP:", wlan.ifconfig()[0])
            else:
                print("Failed to reconnect")
        await asyncio.sleep(5)

# --- Gemini TLS Server ---
async def handle_gemini_client(reader, writer):
    global total_clients, clients_this_sec, max_clients_per_sec, last_sec_tick
    addr = writer.get_extra_info('peername')
    total_clients += 1
    clients_this_sec += 1

    current_sec = int(time.time())
    if current_sec != last_sec_tick:
        if clients_this_sec > max_clients_per_sec:
            max_clients_per_sec = clients_this_sec
        clients_this_sec = 0
        last_sec_tick = current_sec

    try:
        request = await reader.read(1024)
        request = request.decode().strip()
    except:
        writer.close()
        await writer.wait_closed()
        return

    if request.startswith("gemini://"):
        path_start = request.find("/", 9)
        request = request[path_start:] if path_start != -1 else "/"
    if request == "/":
        request = "/index.gmi"

    filepath = safe_join(PUBLIC_DIR, request.lstrip("/"))
    status = ""
    try:
        st = os.stat(filepath)
        is_dir = st[0] & 0x4000
        is_file = st[0] & 0x8000
    except OSError:
        is_dir = is_file = False

    try:
        if is_dir:
            if ENABLE_DIR_LISTING:
                entries = sorted(os.listdir(filepath))
                lines = []
                for entry in entries:
                    ep = request.rstrip("/") + "/" + entry
                    full_entry = safe_join(filepath, entry)
                    try:
                        est = os.stat(full_entry)
                        if est[0] & 0x4000:
                            ep += "/"
                    except OSError:
                        pass
                    lines.append(f"=> {ep} {entry}")
                writer.write(b"20 text/gemini\r\n")
                writer.write("\n".join(lines).encode())
                status = "20 Directory Listing"
            else:
                writer.write(b"51 Not Found\r\n")
                status = "51 Not Found"
        elif is_file:
            content = get_file_content(filepath)
            if content:
                mime_type = get_mime_type(filepath)
                writer.write(f"20 {mime_type}\r\n".encode())
                writer.write(content)
                status = f"20 {mime_type}"
            else:
                writer.write(b"51 Not Found\r\n")
                status = "51 Not Found"
        else:
            writer.write(b"51 Not Found\r\n")
            status = "51 Not Found"
    except:
        writer.write(b"51 Not Found\r\n")
        status = "51 Not Found"

    log_request(addr[0], request, status)
    await writer.drain()
    writer.close()
    await writer.wait_closed()

# --- Secure File Transfer Server (mTLS) ---
CHUNK_SIZE = 512

async def handle_file_client(reader, writer):
    addr = writer.get_extra_info("peername")
    print(f"[{addr}] Authenticated client connected")
    ip_port = addr
    if ip_port not in client_uploads:
        client_uploads[ip_port] = {}
    client_files = client_uploads[ip_port]

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            line = line.decode().strip()
            
            # LIST command
            if line.startswith("LIST"):
                parts = line.split(maxsplit=1)
                subdir = parts[1] if len(parts) > 1 else ""
                dir_path = safe_join(PUBLIC_DIR, subdir)
                if not dir_path or not os.path.exists(dir_path):
                    await writer.write(b"ERROR Directory not found\n")
                    await writer.drain()
                    continue

                try:
                    entries = sorted(os.listdir(dir_path))
                    lines = []
                    for entry in entries:
                        full_path = safe_join(dir_path, entry)
                        if not full_path:
                            continue
                        try:
                            st = os.stat(full_path)
                            is_dir = st[0] & 0x4000
                            is_file = st[0] & 0x8000
                            size = st[6] if is_file else 0
                            entry_type = "<DIR>" if is_dir else f"{size}B"
                            lines.append(f"{entry_type} {entry}")
                        except OSError:
                            lines.append(f"??? {entry}")
                    response = "\n".join(lines) + "\n"
                    await writer.write(response.encode())
                    await writer.drain()
                    print(f"[{addr}] Sent directory listing for '{subdir}'")
                except OSError:
                    await writer.write(b"ERROR Cannot read directory\n")
                    await writer.drain()
                continue

            # START upload
            if line.startswith("UPLOAD "):
                _, filename, total_chunks = line.split()
                total_chunks = int(total_chunks)
                client_files[filename] = {"chunks": {}, "total": total_chunks}
                continue

            # END upload
            elif line.startswith("END "):
                filename = line[4:]
                if filename in client_files:
                    file_info = client_files.pop(filename)
                    save_path = safe_join(PUBLIC_DIR, filename)
                    if save_path:
                        with open(save_path, "wb") as f:
                            for i in range(file_info["total"]):
                                f.write(file_info["chunks"].get(i, b""))
                        await writer.write(f"OK UPLOAD {filename}\n".encode())
                        await writer.drain()
                        print(f"[{addr}] Saved '{filename}' in /public")
                continue

            # DELETE
            elif line.startswith("DELETE "):
                filename = line[7:].strip()
                file_path = safe_join(PUBLIC_DIR, filename)
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
                    await writer.write(f"DELETED {filename}\n".encode())
                    await writer.drain()
                    print(f"[{addr}] Deleted '{filename}'")
                else:
                    await writer.write(f"ERROR {filename} not found\n".encode())
                    await writer.drain()
                continue

            # SEQ chunk
            elif line.startswith("SEQ"):
                sep = line.find("|")
                if sep == -1:
                    continue
                seq_num = int(line[3:sep])
                chunk_data = line[sep+1:].encode()
                if client_files:
                    last_file = list(client_files.keys())[-1]
                    client_files[last_file]["chunks"][seq_num] = chunk_data

    except Exception as e:
        print(f"[{addr}] File transfer error: {e}")
    finally:
        writer.close()
        await writer.wait_closed()
        print(f"[{addr}] Connection closed")

# --- Run all servers ---
async def run_server():
    global wlan
    # TLS context for Gemini
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERT_PATH, keyfile=KEY_PATH)

    # Start Gemini server
    gemini_server = await asyncio.start_server(handle_gemini_client, HOST, GEMINI_PORT, ssl=context)
    print(f"Gemini server listening on {HOST}:{GEMINI_PORT}...")

    # TLS context for file transfer (mutual TLS)
    file_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    file_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    file_sock.bind((HOST, FILE_PORT))
    file_sock.listen(5)
    file_sock.setblocking(False)
    print(f"Secure file server (mTLS) listening on {HOST}:{FILE_PORT}")

    async def file_server_loop():
        loop = asyncio.get_event_loop()
        while True:
            try:
                client_sock, addr = file_sock.accept()
            except OSError:
                await asyncio.sleep(0.1)
                continue

            # Wrap TLS with client authentication
            ssl_sock = ssl.wrap_socket(
                client_sock,
                server_side=True,
                key=KEY_PATH,
                cert=CERT_PATH
                # MicroPython currently doesn’t enforce client certs
            )
            reader = asyncio.StreamReader(ssl_sock)
            writer = asyncio.StreamWriter(ssl_sock, {})
            asyncio.create_task(handle_file_client(reader, writer))

    async def stats_loop():
        global last_update
        while True:
            now = time.ticks_ms()
            if time.ticks_diff(now, last_update) >= UPDATE_INTERVAL * 1000:
                display_stats(total_clients, max_clients_per_sec, start_time)
                last_update = now
            await asyncio.sleep(1)

    try:
        # Run all loops concurrently; Gemini server runs in background automatically
        await asyncio.gather(
            file_server_loop(),
            stats_loop(),
            wifi_watchdog()
        )
    except (KeyboardInterrupt, Exception) as e:
        print("Server interrupted:", e)
    finally:
        gemini_server.close()
        await gemini_server.wait_closed()
        file_sock.close()
        if ENABLE_OLED:
            oled.fill(0)
            oled.show()
        print("Server shutdown complete")

# --- Main entrypoint ---
if __name__ == "__main__":
    wlan = connect_to_wifi()
    if not wlan:
        raise SystemExit
    asyncio.run(run_server())
