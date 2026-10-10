"""Runtimes: locate, verify and drive the llama.cpp / whisper.cpp binaries.

Import the submodules directly -- they are written to be usable as plain files
so a Kaggle cell can ``exec`` one without this package being importable:

    import sys; sys.path.insert(0, "/kaggle/input/runtimes")
    from llamacpp_bridge import LlamaServer, resolve
"""