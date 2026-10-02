"""Disposable /tmp-only archive tooling smoke, runnable in the final image."""
import os
import shutil
import subprocess
import sys
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from PIL import Image

from backend.base.definitions import RAR_EXECUTABLES
from backend.base.files import folder_path
from backend.base.helpers import get_os_type
from backend.implementations.archive_normalization import inspect, normalize


def main():
    fixtures = Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).resolve().parents[1]/'tests/fixtures/archives'
    with TemporaryDirectory(prefix='pullarr-archive-smoke-') as temporary:
        root=Path(temporary); stream=BytesIO()
        Image.new('RGB',(32,64),'white').save(stream,format='PNG')
        page=root/'page.png';page.write_bytes(stream.getvalue())
        for version in (4,5):
            source=root/f'fixture{version}.cbr'
            shutil.copyfile(fixtures/f'synthetic-rar{version}.cbr',source)
            shared=root/f'library{version}.cbr';os.link(source,shared)
            before=source.read_bytes();target=root/f'library{version}.cbz'
            receipt=normalize(str(shared),str(target))
            assert receipt['pages_preserved'] and inspect(str(target))['status']=='healthy'
            assert source.read_bytes()==before and not os.path.samefile(source,target)
            with ZipFile(target) as archive:
                assert sha256(archive.read('1.png')).hexdigest()=='4cc487c54dc29c7f6724beedb0712304091a6e872f2197ff2f7f55c30157c1df'
            shared.unlink();assert source.read_bytes()==before
    print('RAR4/RAR5 reader, verified CBR-to-CBZ, CBZ verification and independent seed-byte preservation PASS; /tmp fixtures only')


if __name__=='__main__':main()
