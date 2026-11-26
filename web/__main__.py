"""python -m web [--host H] [--port P]"""

import argparse

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m web", description="Веб-интерфейс агента проверки требований")
    ap.add_argument("--host", default="127.0.0.1", help="по умолчанию только этот компьютер")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    print(f"Интерфейс: http://{a.host}:{a.port}   API: http://{a.host}:{a.port}/docs")
    uvicorn.run("web.api:app", host=a.host, port=a.port)


if __name__ == "__main__":
    main()
