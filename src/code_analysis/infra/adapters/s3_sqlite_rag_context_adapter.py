"""S3 + SQLite-vec adapter for RAG context retrieval.

Downloads latest/index.db from S3 to a temporary file, loads sqlite-vec,
and executes vector similarity search using the same embedding model as
the rag-indexer.
"""

import logging
import os
import re
import sqlite3
import tempfile
from typing import Any

import botocore.exceptions
from langchain_openai import OpenAIEmbeddings

from code_analysis.domain.ports.rag_context_port import IRagContextPort

LOGGER = logging.getLogger(__name__)

_SUPPORTED_PROVIDERS = {"openai"}


class S3SqliteRagContextAdapter(IRagContextPort):
    """Downloads index.db from S3 and searches via sqlite-vec.

    Gracefully returns [] on any error (missing index, S3 error,
    embedding error, sqlite error).
    """

    def __init__(
        self,
        s3_client: Any,
        bucket_name: str,
        embedding_provider: str | None,
        embedding_model: str | None,
        embedding_api_key: str | None,
    ):
        self._s3 = s3_client
        self._bucket = bucket_name
        self._embedding_provider = embedding_provider
        self._embedding_model = embedding_model
        self._embedding_api_key = embedding_api_key
        self._repository_url: str | None = None
        self._branch: str | None = None
        self._db_path: str | None = None
        self._embeddings: OpenAIEmbeddings | None = None

    # ------------------------------------------------------------------
    # IRagContextPort
    # ------------------------------------------------------------------

    def configure(self, repository_url: str, branch: str) -> None:
        """Set the repository and branch for this job."""
        if self._repository_url != repository_url or self._branch != branch:
            # Different target — discard any cached download
            self.close()
        self._repository_url = repository_url
        self._branch = branch

    def search(self, query: str, k: int) -> list[dict[str, Any]]:
        """Search for k most similar chunks. Returns [] on any error."""
        if not self._repository_url or not self._branch:
            LOGGER.warning(
                "RAG adapter not configured (call configure() first) — skipping"
            )
            return []
        try:
            db_path = self._ensure_db()
            if db_path is None:
                return []
            embedding = self._embed(query)
            if embedding is None:
                return []
            return self._query_db(db_path, embedding, k)
        except Exception:
            LOGGER.warning("RAG context search failed — returning empty", exc_info=True)
            return []

    def close(self) -> None:
        """Delete the temporary index.db file if it was downloaded."""
        if self._db_path and os.path.exists(self._db_path):
            try:
                os.unlink(self._db_path)
                LOGGER.debug("Deleted temporary RAG index at %s", self._db_path)
            except OSError as exc:
                LOGGER.warning("Could not delete temporary RAG index: %s", exc)
            finally:
                self._db_path = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_db(self) -> str | None:
        """Download index.db from S3 if not already downloaded."""
        if self._db_path and os.path.exists(self._db_path):
            return self._db_path

        repo_path = self._build_repo_path(self._repository_url)
        key = f"{repo_path}/branches/{self._branch}/latest/index.db"

        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp_path = tmp.name
            tmp.close()

            LOGGER.info(
                "Downloading RAG index s3://%s/%s → %s",
                self._bucket,
                key,
                tmp_path,
            )
            self._s3.download_file(self._bucket, key, tmp_path)
            self._db_path = tmp_path
            return tmp_path

        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey"):
                LOGGER.info(
                    "RAG index not found for %s@%s — skipping enrichment",
                    self._repository_url,
                    self._branch,
                )
            else:
                LOGGER.warning("S3 error downloading RAG index: %s", exc)
            return None

    def _embed(self, text: str) -> list[float] | None:
        """Generate an embedding vector for the query text."""
        if (
            not self._embedding_provider
            or not self._embedding_model
            or not self._embedding_api_key
        ):
            LOGGER.warning(
                "Embedding config missing (provider/model/key) — skipping RAG"
            )
            return None

        provider = self._embedding_provider.lower()
        if provider not in _SUPPORTED_PROVIDERS:
            LOGGER.warning(
                "Unsupported embedding provider: %s", self._embedding_provider
            )
            return None

        try:
            if self._embeddings is None:
                self._embeddings = OpenAIEmbeddings(
                    model=self._embedding_model,
                    api_key=self._embedding_api_key,
                )
            result = self._embeddings.embed_documents([text])
            return result[0] if result else None
        except Exception:
            LOGGER.warning(
                "Embedding generation failed — skipping RAG enrichment", exc_info=True
            )
            return None

    def _query_db(
        self, db_path: str, embedding: list[float], k: int
    ) -> list[dict[str, Any]]:
        """Run vector similarity search against the SQLite-vec index."""
        try:
            import sqlite_vec  # lazy import: graceful if not installed
        except ImportError:
            LOGGER.warning(
                "sqlite_vec not installed — RAG context unavailable. "
                "Install sqlite-vec or rebuild the agent image."
            )
            return []

        conn = sqlite3.connect(db_path)
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)

            cursor = conn.cursor()
            # vec0 KNN queries require k = ? in WHERE (not just LIMIT ?)
            # when selecting auxiliary columns alongside the vector column.
            cursor.execute(
                """
                SELECT file_path, chunk_text, distance
                FROM chunks
                WHERE embedding MATCH ?
                  AND k = ?
                ORDER BY distance
                """,
                (sqlite_vec.serialize_float32(embedding), k),
            )
            rows = cursor.fetchall()
            return [
                {"file_path": row[0], "chunk_text": row[1], "distance": row[2]}
                for row in rows
            ]
        except Exception:
            LOGGER.warning("sqlite-vec query failed — returning empty", exc_info=True)
            return []
        finally:
            conn.close()

    @staticmethod
    def _build_repo_path(repository_url: str) -> str:
        url = re.sub(r"^https?://", "", repository_url)
        return url.rstrip("/").replace(".git", "")
