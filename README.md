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