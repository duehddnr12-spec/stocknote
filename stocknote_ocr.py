"""Files used by the bundled, offline OCR worker."""
from pathlib import Path
from shutil import copyfile


CORE_FILES = (
    'tesseract-core-lstm.wasm.js',
    'tesseract-core-simd-lstm.wasm.js',
    'tesseract-core-relaxedsimd-lstm.wasm.js',
)
LANGUAGES = ('kor', 'eng')
CONTENT_TYPES = {
    **dict.fromkeys(('receipt.mjs', 'tesseract.min.js', 'worker.min.js', *CORE_FILES), 'text/javascript; charset=utf-8'),
    **dict.fromkeys((f'{lang}.traineddata.gz' for lang in LANGUAGES), 'application/octet-stream'),
    'OCR-LICENSES.txt': 'text/plain; charset=utf-8',
}


def stage_assets(root, target):
    root, target = Path(root), Path(target)
    modules = root / 'ocr' / 'node_modules'
    sources = {
        'receipt.mjs': root / 'stocknote_photo.mjs',
        **{name: modules / 'tesseract.js' / 'dist' / name for name in ('tesseract.min.js', 'worker.min.js')},
        **{name: modules / 'tesseract.js-core' / name for name in CORE_FILES},
        **{f'{lang}.traineddata.gz': modules / '@tesseract.js-data' / lang / '4.0.0_best_int' / f'{lang}.traineddata.gz' for lang in LANGUAGES},
    }
    target.mkdir(parents=True, exist_ok=True)
    for name, source in sources.items():
        copyfile(source, target / name)

    notices = [
        'StockNote OCR: Tesseract.js and Tesseract.js-core.\n'
        'Korean and English Tesseract trained data: https://github.com/tesseract-ocr/tessdata_best (Apache 2.0).\n'
        'Data packages: https://github.com/naptha/tessdata.\n'
    ]
    for package in sorted(modules.iterdir()):
        if package.is_dir():
            for license_file in sorted(package.iterdir()):
                if license_file.is_file() and license_file.name.upper().startswith(('LICENSE', 'NOTICE', 'COPYING')):
                    notices.append(f'\n{package.name} / {license_file.name}\n{license_file.read_text(encoding="utf-8", errors="replace")}')
    for asset in ('tesseract.min.js', 'worker.min.js'):
        license_file = modules / 'tesseract.js' / 'dist' / f'{asset}.LICENSE.txt'
        notices.append(f'\n{asset}\n{license_file.read_text(encoding="utf-8")}')
    (target / 'OCR-LICENSES.txt').write_text('\n'.join(notices), encoding='utf-8')
