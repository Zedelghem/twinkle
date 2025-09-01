# twinkle
A minimal Gemini Protocol server for microcontrollers. Written in micropython, tested on Raspberry Pico W.

Takes care of basic TLS handshakes and exposes the contents of the /public directory using a non-blocking socket. That's it.

Set the network credentials and upload your TLS certificate/key pair in .der format to the root folder. Copy your .gmi files into /public and have yourself a working gemini server.