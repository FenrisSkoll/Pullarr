# Synthetic RAR fixtures

Created for this project from generated 32×64 white PNGs and original minimal
ComicInfo XML. No comic content or user data. RAR4/RAR5 three-page fixtures have
members `1.png`, `2.png`, `10.png`, `ComicInfo.xml`; 25/250-page fixtures contain
zero-padded PNG names. Stored RAR output was generated with the pre-existing RAR
6.24 tool (`a -ma4/-ma5 -m0 -ep1`), not by reconstructing its compression algorithm.
That tool is not included or needed to run the tests. RAR's archive output clause
permits archive distribution without additional royalties; page/XML payloads
are original test material under the project license.

SHA-256 and payload provenance are recorded in `licenses/vendor-manifest.json`.
The fixtures are immutable input data, not a production comic library. Readers
are tested with UnRAR 7.23. To change fixtures, generate new synthetic payloads
with a lawfully obtained writer and review every member; never substitute comics.
