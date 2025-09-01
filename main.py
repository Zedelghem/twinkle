import socket
import ssl
import network
import time
import select

from wifisetup import *

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

def run_gemini_server(cert_path="/certificate.der.crt",
                      key_path="/private.key.der",
                      index_path="public/index.gmi",
                      host="0.0.0.0",
                      port=1965):
    # Load certificate, key, and index file
    with open(cert_path, "rb") as f:
        cert_bytes = f.read()
    with open(key_path, "rb") as f:
        key_bytes = f.read()
    with open(index_path, "rb") as f:
        index_content = f.read()

    # Create non-blocking server socket
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(5)
    server.setblocking(False)
    print(f"Gemini server listening on {host}:{port}...")

    clients = {}  # map client socket -> TLS socket

    while True:
        # Accept new connections
        try:
            client, addr = server.accept()
            print("New connection from", addr)
            client.setblocking(False)
            # Wrap immediately with TLS
            tls_client = ssl.wrap_socket(client, server_side=True, key=key_bytes, cert=cert_bytes)
            clients[client] = tls_client
        except OSError:
            pass  # no new connection

        # Handle existing clients
        for client, tls_client in list(clients.items()):
            try:
                r, _, _ = select.select([tls_client], [], [], 0)
                if r:
                    request = tls_client.readline(1024).decode().strip()
                    print("Request:", request)
                    tls_client.write(b"20 text/gemini\r\n")
                    tls_client.write(index_content)
                    tls_client.close()
                    client.close()
                    del clients[client]
            except Exception as e:
                print("Client error:", e)
                try:
                    tls_client.close()
                    client.close()
                except:
                    pass
                del clients[client]

# --- Main ---
if __name__ == "__main__":
    wlan = connect_to_wifi()
    if not wlan:
        raise SystemExit
    run_gemini_server()