"""Impala에서 조회한 결과를 Greenplum으로 적재하는 ETL 패키지."""

from .config import GreenplumConfig, ImpalaConfig, JobConfig, S3Config, load_config
from .pipeline import LoadResult, run_load
from .s3_stage import S3Stager, render_gp_s3_config

__all__ = [
    "GreenplumConfig",
    "ImpalaConfig",
    "JobConfig",
    "LoadResult",
    "S3Config",
    "S3Stager",
    "load_config",
    "render_gp_s3_config",
    "run_load",
]

__version__ = "0.1.0"
