# twinkle
A minimal Gemini Protocol server for microcontrollers. Written in micropython 1.2.6, tested on Raspberry Pico W.

Set the network credentials and upload your TLS certificate/key pair in .der format to the root folder. Copy your .gmi files into /public and have yourself a working gemini server.

Takes care of basic TLS handshakes and exposes the contents of the /public directory using a non-blocking socket. That's it. Some basic quality-of-life features:

- simple caching to reduce flash reads
- automatic cache invalidation (to update without rebooting)
- logging (of files, directory listings, errors, connections)
- Supports subdirectories and files everywhere under public/
- optional directory listing with links
- preventing unwanted directory traversal
- .gmi → text/gemini, .txt → text/plain.
- Other files use MIME map or default application/octet-stream.
- Auto Wi-fi reconnect
- Graceful shutdown
- SSD1306 Oled display code included (optional; currently hardcoded for pins 16 and 17 for SDA and SDL)
- Secure file transfer & delete server over TLS (for remote file management)
- Mutual TLS (mTLS) requiring client certificates for any file operation (shared secret)
- Supports chunked uploads, delete and list commands.

## Caveats

1. The certificate files on the RPico must be in the .der format. I just couldn't get .pem to work. Not sure why. 

2. The key on the pico must be converted to the rsa -traditional format; i.e., the file must start with === BEGIN RSA PRIVATE KEY ===.

3. The key of the client can be in .pem.


4. If you get errors about certificate being too weak or similar, make sure your key is 2048+ bit long.

5. The upload "client" is still under development