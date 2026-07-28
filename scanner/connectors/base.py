"""The common interface. A connector = one source (or family of sources).

Add a new source by writing a class with `.fetch() -> list[NormalizedJob]`
and registering it in the pipeline. Nothing else in the codebase changes.
That is what keeps coverage expandable without narrowing anything.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from scanner.models import NormalizedJob

log = logging.getLogger(__name__)


class Connector(ABC):
    name: str = "base"

    @abstractmethod
    def fetch(self) -> list[NormalizedJob]:
        """Return raw postings from this source, already normalized.

        Connectors should be defensive: on any network/auth error, log and
        return [] rather than raising, so one flaky source never kills the run.
        """
        raise NotImplementedError
