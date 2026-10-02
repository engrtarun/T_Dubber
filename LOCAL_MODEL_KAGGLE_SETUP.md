# Local translation model on Kaggle

The dubbing worker runs an Index-Homura model through a local vLLM OpenAI-compatible endpoint. No hosted LLM key is required.

The first run installs vLLM and downloads Homura-2B, which can take several minutes. On Kaggle T4 x2, the worker runs Homura-2B on GPU 0 and reserves GPU 1 for speech recognition and synthesis so all stages fit in memory. Model startup can take up to 20 minutes. Enable Internet in Kaggle notebook settings so the runtime can download packages and model weights.

Mazinger receives `--openai-base-url http://localhost:8000/v1` and `--openai-api-key EMPTY`. Screenshot context is disabled because this translation model accepts text inputs.
