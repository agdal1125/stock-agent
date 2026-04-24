"""DB schema 초기화 (멱등)."""
from ..db import init_db
from ..config import CFG


def main() -> None:
    init_db()
    print(f"[init_db] schema ready at {CFG.db_path}")


if __name__ == "__main__":
    main()
