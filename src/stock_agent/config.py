from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _root() -> Path:
    # config.py lives in src/stock_agent/; project root is two up
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Config:
    openai_api_key: str
    openai_model: str
    openai_embed_model: str
    kill_switch: bool
    data_dir: Path
    seed_dir: Path
    db_path: Path
    cache_dir: Path
    agent_int_port: int
    source_ext_port: int
    eager_top_n: int
    # P7 — 비용 제어
    price_input_per_m: float          # USD per 1M input tokens
    price_output_per_m: float         # USD per 1M output tokens
    price_cached_input_per_m: float   # USD per 1M cached input tokens
    price_embed_per_m: float          # USD per 1M embedding tokens
    daily_usd_cap: float              # 0 = 무제한

    @classmethod
    def load(cls) -> "Config":
        root = _root()
        data_dir = Path(os.getenv("STOCK_AGENT_DATA_DIR", root / "data")).resolve()
        seed_dir = Path(os.getenv("STOCK_AGENT_SEED_DIR", root / "seed")).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "raw").mkdir(parents=True, exist_ok=True)
        (data_dir / "cache").mkdir(parents=True, exist_ok=True)
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
            openai_embed_model=os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small"),
            kill_switch=os.getenv("LLM_KILL_SWITCH", "0") == "1",
            data_dir=data_dir,
            seed_dir=seed_dir,
            db_path=data_dir / "canonical.db",
            cache_dir=data_dir / "cache",
            agent_int_port=int(os.getenv("AGENT_INT_PORT", "8001")),
            source_ext_port=int(os.getenv("SOURCE_EXT_PORT", "8002")),
            eager_top_n=int(os.getenv("EAGER_TOP_N", "5")),
            price_input_per_m=float(os.getenv("PRICE_INPUT_PER_M", "0.75")),
            price_output_per_m=float(os.getenv("PRICE_OUTPUT_PER_M", "4.5")),
            price_cached_input_per_m=float(os.getenv("PRICE_CACHED_INPUT_PER_M", "0.075")),
            price_embed_per_m=float(os.getenv("PRICE_EMBED_PER_M", "0.02")),
            daily_usd_cap=float(os.getenv("DAILY_USD_CAP", "0")),
        )


CFG = Config.load()
