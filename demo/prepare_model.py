from huggingface_hub import hf_hub_download

from .semantic import MODEL_ASSETS, MODEL_DIR, MODEL_REPO, MODEL_REVISION


def main():
    for asset in MODEL_ASSETS:
        hf_hub_download(MODEL_REPO, asset, revision=MODEL_REVISION, local_dir=MODEL_DIR, token=False)
    print(f"Модель загружена в {MODEL_DIR}")


if __name__ == "__main__":
    main()
