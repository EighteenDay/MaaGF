from pathlib import Path

import shutil

assets_dir = Path(__file__).parent.parent.resolve() / "assets"


def configure_ocr_model():
    ocr_dir = assets_dir / "resource" / "model" / "ocr"

    # 已经有 OCR 模型就直接用，不再依赖 MaaCommonAssets 子模块
    if ocr_dir.exists():
        print("Found existing OCR directory, skipping default OCR model import.")
        return

    assets_ocr_dir = assets_dir / "MaaCommonAssets" / "OCR"
    if not assets_ocr_dir.exists():
        print(f"File Not Found: {assets_ocr_dir}")
        print(
            "请先拉取子模块：git submodule update --init --depth 1 assets/MaaCommonAssets"
        )
        print(
            "或手动把 ppocr_v6 模型解压到 assets/resource/model/ocr/ 后重试。"
        )
        exit(1)

    shutil.copytree(
        assets_ocr_dir / "ppocr_v6" / "small",
        ocr_dir,
        dirs_exist_ok=True,
    )


if __name__ == "__main__":
    configure_ocr_model()

    print("OCR model configured.")
