from langchain_core.documents import Document

from rag.normalize import Place


def places_to_documents(places: list[Place]) -> list[Document]:
    return [
        Document(page_content=place.page_content, metadata=place.metadata)
        for place in places
    ]
