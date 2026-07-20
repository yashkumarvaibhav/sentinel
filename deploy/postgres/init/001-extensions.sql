-- Incident similarity ("we have seen this before") is a vector search over
-- incident embeddings, so pgvector is part of the base image and enabled here.
CREATE EXTENSION IF NOT EXISTS vector;
