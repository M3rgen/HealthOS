"""The payload names archive parsers need, independent of transport and credentials.

An API descriptor may extend this value with scopes, filters and pagination rules.
Archive identity, normalization and read projections only need the record's name.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RecordType:
    name: str

    @property
    def payload_key(self) -> str:
        return self.name.replace("-", "_")

    @property
    def json_key(self) -> str:
        first, *rest = self.name.split("-")
        return first + "".join(word.title() for word in rest)
