from sources.base import Chunk


def test_chunk_metadata_defaults_to_empty_dict():
    c = Chunk(window_start="2024-01-15T14:00:00", text="hello", source="test")
    assert c.metadata == {}


def test_chunk_metadata_accepts_arbitrary_keys():
    c = Chunk(
        window_start="2024-01-15T14:00:00",
        text="hello",
        source="test",
        metadata={"lat": 40.7128, "lon": -74.006},
    )
    assert c.metadata["lat"] == 40.7128
    assert c.metadata["lon"] == -74.006
