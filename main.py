import argparse
from bot.engine import BotEngine


def main():
    parser = argparse.ArgumentParser(description="Discord Activity Bot")
    parser.add_argument("--vision", action="store_true", help="Enable bot vision debug window")
    args = parser.parse_args()

    engine = BotEngine(
        window_title="Discord",
        show_debug=args.vision
    )
    engine.start()


if __name__ == "__main__":
    main()
