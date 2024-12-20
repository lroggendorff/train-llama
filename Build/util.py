import os
from huggingface_hub import HfApi

class Conclusion(Exception):
    def __init__(self, message="Script execution has completed."):
        self.message = message
        super().__init__(self.message)

class Space:
    def __init__(self):
        self.api = HfApi()
        self.pause = lambda: self.api.pause_space(f"{os.getenv('SPACE_AUTHOR_NAME')}/{os.getenv('SPACE_REPO_NAME')}")

    def stop(self, message=None):
        if message:
            raise Conclusion(f"{type(message).__name__}: {message}")
        raise Conclusion()
