"""Impala에서 조회한 결과를 Greenplum으로 적재하는 ETL 패키지."""

from .config import GreenplumConfig, ImpalaConfig, JobConfig, load_config
from .pipeline import LoadResult, run_load

__all__ = [
    "GreenplumConfig",
    "ImpalaConfig",
    "JobConfig",
    "LoadResult",
    "load_config",
    "run_load",
]

__version__ = "0.1.0"
