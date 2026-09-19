"""The RAG package must remain importable without credentials or network access."""


def test_rag_modules_import_offline() -> None:
    from eventide import rag
    from eventide.rag import chunker, embedding, evaluation, index, search, tool

    assert rag.__doc__
    assert all(
        module.__doc__ for module in (chunker, embedding, evaluation, index, search, tool)
    )
