import socket
import ssl
import network
import time
import os
import select

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

file_cache = {}

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

# --- Logging ---
def log_request(client_addr, request, status):
    t = time.localtime()
    timestamp = "{:04}-{:02}-{:02} {:02}:{:02}:{:02}".format(
        t[0], t[1], t[2], t[3], t[4], t[5]
    )
    print(f"[{timestamp}] {client_addr} -> {request} ({status})")

# --- Caching ---
def get_file_content(filepath):
    try:
        st = os.stat(filepath)
        mtime = st[8]  # modification time
    except OSError:
        return None

    # Check cache
    if CACHE_ENABLED and filepath in file_cache:
        cached_content, cached_mtime = file_cache[filepath]
        if cached_mtime == mtime:
            return cached_content  # cache still valid

    # Read file from disk
    try:
        with open(filepath, "rb") as f:
            content = f.read()
        if CACHE_ENABLED:
            if len(file_cache) >= CACHE_MAX_SIZE:
                # Simple eviction: remove oldest cached item
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

# --- Read Gemini request safely ---
def read_request(tls_client, maxlen=1024):
    buf = b""
    while b"\r\n" not in buf and len(buf) < maxlen:
        try:
            chunk = tls_client.read(1)
            if not chunk:
                break
            buf += chunk
        except OSError:
            break
    return buf.decode().strip()

# --- Gemini server ---
def run_gemini_server(cert_path=CERT_PATH, key_path=KEY_PATH):
    with open(cert_path, "rb") as f:
        cert_bytes = f.read()
    with open(key_path, "rb") as f:
        key_bytes = f.read()

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(5)
    server.setblocking(False)
    print(f"Gemini server listening on {HOST}:{PORT}...")

    clients = {}
    client_addrs = {}

    while True:
        try:
            client, addr = server.accept()
            print("New connection from", addr)
            client.setblocking(False)
            tls_client = ssl.wrap_socket(client, server_side=True, key=key_bytes, cert=cert_bytes)
            clients[client] = tls_client
            client_addrs[client] = addr[0]
        except OSError:
            pass

        for client, tls_client in list(clients.items()):
            try:
                r, _, _ = select.select([tls_client], [], [], 0)
                if r:
                    request = read_request(tls_client)
                    if not request:
                        continue
                    client_ip = client_addrs[client]

                    # Strip gemini://host part if present
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

                    if not filepath:
                        tls_client.write(b"59 Bad Request\r\n")
                        status = "59 Bad Request"
                    else:
                        try:
                            st = os.stat(filepath)
                            is_dir = st[0] & 0x4000
                            is_file = st[0] & 0x8000
                        except OSError:
                            is_dir = False
                            is_file = False

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
                                tls_client.write(b"20 text/gemini\r\n")
                                tls_client.write("\n".join(lines).encode())
                                status = "20 Directory Listing"
                            else:
                                tls_client.write(b"51 Not Found\r\n")
                                status = "51 Not Found"

                        elif is_file:
                            content = get_file_content(filepath)
                            if content:
                                mime_type = get_mime_type(filepath)
                                tls_client.write(f"20 {mime_type}\r\n".encode())
                                tls_client.write(content)
                                status = f"20 {mime_type}"
                            else:
                                tls_client.write(b"51 Not Found\r\n")
                                status = "51 Not Found"

                        else:
                            tls_client.write(b"51 Not Found\r\n")
                            status = "51 Not Found"

                    log_request(client_ip, request, status)
                    tls_client.close()
                    client.close()
                    del clients[client]
                    del client_addrs[client]

            except Exception as e:
                print("Client error:", e)
                try:
                    tls_client.close()
                    client.close()
                except:
                    pass
                if client in clients:
                    del clients[client]
                if client in client_addrs:
                    del client_addrs[client]

# --- Main ---
if __name__ == "__main__":
    wlan = connect_to_wifi()
    if not wlan:
        raise SystemExit
    run_gemini_server()
