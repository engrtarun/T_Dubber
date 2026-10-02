import os

class FileChunkIO:
    def __init__(self, filepath, offset, length, chunk_name):
        self.filepath = filepath
        self.offset = offset
        self.length = length
        self.name = chunk_name
        self.f = open(filepath, 'rb')
        self.f.seek(offset)
        self.read_bytes = 0

    def read(self, size=-1):
        if self.read_bytes >= self.length:
            return b''
        if size == -1 or size > (self.length - self.read_bytes):
            size = int(self.length - self.read_bytes)
        data = self.f.read(size)
        self.read_bytes += len(data)
        return data

    def seek(self, offset, whence=0):
        if whence == 0:
            self.read_bytes = offset
        elif whence == 1:
            self.read_bytes += offset
        elif whence == 2:
            self.read_bytes = self.length + offset
        self.f.seek(self.offset + self.read_bytes)
        return self.read_bytes

    def tell(self):
        return self.read_bytes
        
    def close(self):
        self.f.close()

with open("test_chunk.txt", "wb") as f:
    f.write(b"Hello this is a test file for chunking logic")

chunk = FileChunkIO("test_chunk.txt", 6, 10, "chunk1")
print(chunk.read(4))
print(chunk.read())
chunk.seek(0)
print(chunk.read())
chunk.close()
