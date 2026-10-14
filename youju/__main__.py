import os
import threading
import webbrowser

import uvicorn


def main() -> None:
    host = os.getenv("YOUJU_HOST", "127.0.0.1")
    port = int(os.getenv("YOUJU_PORT", "8000"))
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{shown}:{port}"
    print(f"YouJu is starting: {url}")
    if os.getenv("YOUJU_OPEN_BROWSER", "1") == "1":
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("youju.main:app", host=host, port=port)


if __name__ == "__main__":
    main()
