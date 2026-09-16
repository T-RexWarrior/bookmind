"""Parser package — DocumentParser providers and the provider registry."""

from __future__ import annotations

from .base import DocumentParser, FileMetadata, ParseOptions, ParserHealth, select_parser
from .basic_pdf import PlainPdfFallback
from .mineru import MinerUParser
from .pypdf_parser import PyPdfParser
from .rapidocr_parser import RapidOcrParser
from .ppstructure import PpStructureParser
from .adaptive import AdaptivePdfParser

__all__ = [
    "DocumentParser",
    "FileMetadata",
    "ParseOptions",
    "ParserHealth",
    "select_parser",
    "PlainPdfFallback",
    "MinerUParser",
    "PyPdfParser",
    "RapidOcrParser",
    "PpStructureParser",
    "AdaptivePdfParser",
]
