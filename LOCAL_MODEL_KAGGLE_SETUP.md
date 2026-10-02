# Local translation model on Kaggle

The dubbing worker runs an Index-Homura model through a local vLLM OpenAI-compatible endpoint. No hosted LLM key is required.

The first run installs vLLM and downloads the model, which can take several minutes. The worker tries the 9B checkpoint and falls back to the 2B checkpoint if the server does not become ready. Enable Internet in Kaggle notebook settings so the runtime can download packages and model weights.

Mazinger receives `--openai-base-url http://localhost:8000/v1` and `--openai-api-key EMPTY`. Screenshot context is disabled because this translation model accepts text inputs.
