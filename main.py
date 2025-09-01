import uasyncio as asyncio
import socket
import ssl
import network
import time
import os
import gc
import machine
from wifisetup import *

# --- Settings ---
ENABLE_DIR_LISTING = True
PUBLIC_DIR = "/public"
CERT_PATH = "/certificate.der.crt"
KEY_PATH = "/private.key.der"
HOST = "0.0.0.0"
PORT = 1965
CACHE_ENABLED = True
CACHE_MAX_SIZE = 50

READ_BUFFER_SIZE = 512
FILE_BUFFER_SIZE = 1024

file_cache = {}

# --- Monitoring ---
start_time = time.ticks_ms()
total_clients = 0
max_clients_per_sec = 0
clients_this_sec = 0
last_sec_tick = int(time.time())
UPDATE_INTERVAL = 5  # seconds
last_update = time.ticks_ms()

# --- Helper functions ---
def read_chip_temp():
    sensor = machine.ADC(4)
    reading = sensor.read_u16()
    voltage = reading * 3.3 / 65535
    temp_c = 27 - (voltage - 0.706) / 0.001721
    return temp_c

def print_stats():
    global last_update
    gc.collect()
    elapsed_ms = time.ticks_diff(time.ticks_ms(), start_time)
    elapsed_sec = int(elapsed_ms / 1000)
    hours = elapsed_sec // 3600
    minutes = (elapsed_sec % 3600) // 60
    seconds = elapsed_sec % 60
    avg_clients_per_sec = total_clients / elapsed_sec if elapsed_sec > 0 else 0
    temp_c = read_chip_temp()

    print("=== Gemini Server Stats ===")
    print("Total runtime: {:02}h {:02}m {:02}s".format(hours, minutes, seconds))
    print("Total clients:", total_clients)
    print("Average clients/sec: {:.3f}".format(avg_clients_per_sec))
    print("Max clients/sec:", max_clients_per_sec)
    print("Free memory:", gc.mem_free())
    print("Chip temperature: {:.2f}°C".format(temp_c))
    print("===================")
    last_update = time.ticks_ms()

# --- WiFi ---
def connect_to_wifi():
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

# --- MIME ---
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

# --- Caching ---
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

# --- Safe path join ---
def safe_join(base, *paths):
    full = "/".join([base.strip("/")] + [p.strip("/") for p in paths])
    if ".." in full:
        return None
    return full

# --- Logging ---
def log_request(client_addr, request, status):
    t = time.localtime()
    timestamp = "{:04}-{:02}-{:02} {:02}:{:02}:{:02}".format(
        t[0], t[1], t[2], t[3], t[4], t[5]
    )
    print(f"[{timestamp}] {client_addr} -> {request} ({status})")

# --- Handle client ---
async def handle_client(reader, writer):
    global total_clients, clients_this_sec, max_clients_per_sec, last_sec_tick

    addr = writer.get_extra_info('peername')
    total_clients += 1
    clients_this_sec += 1

    # Track max clients/sec
    current_sec = int(time.time())
    if current_sec != last_sec_tick:
        if clients_this_sec > max_clients_per_sec:
            max_clients_per_sec = clients_this_sec
        clients_this_sec = 0
        last_sec_tick = current_sec

    # Read request
    try:
        request = await reader.read(1024)
        request = request.decode().strip()
    except Exception as e:
        print("Read error:", e)
        writer.close()
        await writer.wait_closed()
        return

    # Parse request
    if request.startswith("gemini://"):
        path_start = request.find("/", 9)
        if path_start != -1:
            request = request[path_start:]
        else:
            request = "/"
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
    except Exception as e:
        print("File send error:", e)

    log_request(addr[0], request, status)

    await writer.drain()
    writer.close()
    await writer.wait_closed()

# --- Server ---
async def run_server():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERT_PATH, keyfile=KEY_PATH)

    server = await asyncio.start_server(handle_client, HOST, PORT, ssl=context)
    print(f"Gemini server listening on {HOST}:{PORT}...")

    # Stats update loop
    async def stats_loop():
        global last_update
        while True:
            now = time.ticks_ms()
            if time.ticks_diff(now, last_update) >= UPDATE_INTERVAL * 1000:
                print_stats()
            await asyncio.sleep(1)

    # Just run stats_loop forever; server is already listening in background
    await stats_loop()

# --- Main ---
if __name__ == "__main__":
    wlan = connect_to_wifi()
    if not wlan:
        raise SystemExit
    asyncio.run(run_server())
