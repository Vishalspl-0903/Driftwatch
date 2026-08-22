"""drift/embed.py -- image and text embeddings for catalog-centroid scoring.

Model choices, and why:

* Images: CLIP ViT-B/32 (openai/clip-vit-base-patch32) via `transformers`.
  Picked over SigLIP for this environment specifically: `transformers`
  exposes CLIP's vision tower through a single well-documented
  CLIPModel/CLIPProcessor pair with no extra config, it's a ~600MB one-time
  download, and CLIP's contrastive image embedding space is exactly the
  "which images are semantically alike" property this needs -- centroid
  distance, not zero-shot classification, is the use case, and SigLIP's
  sigmoid-loss objective is tuned for the latter, not this. If image
  retrieval quality turns out to matter more than embedding-space cleanliness
  once there's a bigger dataset, SigLIP is the natural upgrade.

* Text: BAAI/bge-small-en-v1.5 via `sentence-transformers`. Small (~130MB),
  purpose-built for embedding similarity rather than generation, and
  sentence-transformers' `.encode()` is the shortest path from a list of
  strings to comparable vectors. BGE's asymmetric query-instruction prefix
  ("Represent this sentence for searching relevant passages") is a retrieval
  convention that doesn't apply here -- both sides of the comparison are the
  same kind of text (catalog/nav phrases vs. catalog/nav phrases), a
  symmetric case, so no instruction prefix is used.

Both models are loaded lazily and cached as module-level singletons so
run_eval.py's 17-domain loop pays the load cost once, not per pair.
"""

from __future__ import annotations

import io
from functools import lru_cache

import numpy as np
from PIL import Image

CLIP_MODEL_NAME = "openai/clip-vit-base-patch32"
TEXT_MODEL_NAME = "BAAI/bge-small-en-v1.5"


@lru_cache(maxsize=1)
def _clip():
    import torch
    from transformers import CLIPModel, CLIPProcessor

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = CLIPModel.from_pretrained(CLIP_MODEL_NAME).to(device).eval()
    processor = CLIPProcessor.from_pretrained(CLIP_MODEL_NAME)
    return model, processor, device


@lru_cache(maxsize=1)
def _text_model():
    from sentence_transformers import SentenceTransformer

    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    return SentenceTransformer(TEXT_MODEL_NAME, device=device)


def embed_images(image_bytes_list: list[bytes], batch_size: int = 16) -> np.ndarray:
    """Return an (n, d) L2-normalized array. Bytes that fail to decode as an
    image are dropped silently -- a corrupt/partial fetch is exactly the kind
    of thing fetch.py's MIN_BYTES floor mostly catches, but PIL is the final
    ground truth on "is this actually a decodable image"."""
    import torch

    model, processor, device = _clip()
    images = []
    for b in image_bytes_list:
        try:
            images.append(Image.open(io.BytesIO(b)).convert("RGB"))
        except Exception:
            continue
    if not images:
        return np.zeros((0, model.config.projection_dim), dtype=np.float32)

    vecs = []
    with torch.no_grad():
        for i in range(0, len(images), batch_size):
            batch = images[i : i + batch_size]
            inputs = processor(images=batch, return_tensors="pt").to(device)
            # transformers >= 5.x returns the vision-tower output object rather than a
            # bare tensor; .pooler_output is the CLIP-projected embedding (get_image_features
            # overwrites it with the projected value before returning -- see its source).
            feats = model.get_image_features(**inputs).pooler_output
            feats = feats / feats.norm(p=2, dim=-1, keepdim=True)
            vecs.append(feats.cpu().numpy())
    return np.concatenate(vecs, axis=0)


def embed_texts(texts: list[str]) -> np.ndarray:
    """Return an (n, d) L2-normalized array."""
    if not texts:
        model = _text_model()
        return np.zeros((0, model.get_sentence_embedding_dimension()), dtype=np.float32)
    model = _text_model()
    vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return np.asarray(vecs, dtype=np.float32)


def centroid(vectors: np.ndarray) -> np.ndarray | None:
    if vectors.shape[0] == 0:
        return None
    c = vectors.mean(axis=0)
    norm = np.linalg.norm(c)
    return c / norm if norm > 0 else c


def cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    sim = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
    sim = max(-1.0, min(1.0, sim))
    return 1.0 - sim
