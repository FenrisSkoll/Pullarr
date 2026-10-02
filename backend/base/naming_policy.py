"""Explicit immutable inputs for the existing naming policy."""

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class NamingSettings:
    replace_illegal_characters: bool
    volume_folder_naming: str
    file_naming: str
    file_naming_empty: str
    file_naming_special_version: str
    file_naming_vai: str
    long_special_version: bool
    volume_padding: int
    issue_padding: int

    @classmethod
    def capture(cls, settings: object) -> 'NamingSettings':
        return cls(**{f.name: getattr(settings, f.name) for f in fields(cls)})
