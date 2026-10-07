"""Bedrock Knowledge Base retrieval client (Task 7, REQ-B-1).

Thin ``semantic_search`` over the Bedrock Knowledge Base ``Retrieve`` API, bound to the
Task 6 S3 Vectors index. Orchestration stays in application code (Retrieve, not
RetrieveAndGenerate); the client only parses retrieval results into enriched-movie
records carrying the vector metadata (title/genres/sentiment/mood/pes).
"""
