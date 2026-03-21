from codex_utils import CodexClient


def main() -> None:
    client = CodexClient()
    reply = client.ask("Balas satu kata saja: halo")
    print(reply)


if __name__ == "__main__":
    main()
