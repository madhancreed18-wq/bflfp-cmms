import socket
import uvicorn

from server.config import PORT

if __name__ == "__main__":
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "localhost"
    print(f"\n  BFLFP CMMS running:\n   This PC : http://localhost:{PORT}\n"
          f"   Phones  : http://{ip}:{PORT}  (same wifi)\n")
    uvicorn.run("server.app:app", host="0.0.0.0", port=PORT)
