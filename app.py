import gradio as gr
import os
from pipeline import run_pipeline

def start_dubbing(video, target_lang, speaker_detection):
    if not video:
        return "Error: Please upload a video first."
    
    # In Gradio, video could be a filepath depending on the component type
    # If it's a temp file, we get its name
    movie_name = "my_movie"
    if isinstance(video, str):
        movie_name = os.path.splitext(os.path.basename(video))[0]
        
    project_dir = os.path.join("projects", movie_name)
    os.makedirs(project_dir, exist_ok=True)
    
    # Start the orchestrator pipeline
    status = run_pipeline(video, project_dir, target_lang, speaker_detection)
    return status

with gr.Blocks(title="AI Movie Dubbing Orchestrator", theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 🎬 AI Movie Dubbing Control Panel")
    gr.Markdown("Upload a video, choose your language, and let the cloud GPU do the heavy lifting!")
    
    with gr.Row():
        with gr.Column():
            video_input = gr.Video(label="Upload Video")
            target_language = gr.Dropdown(choices=["Hindi", "English", "Spanish", "French", "German"], value="Hindi", label="Target Language")
            speaker_toggle = gr.Checkbox(label="Enable Speaker Detection", value=True)
            start_button = gr.Button("🚀 Start Dubbing", variant="primary")
            
        with gr.Column():
            status_output = gr.Textbox(label="Status / Logs", lines=12, interactive=False)
            
    start_button.click(
        fn=start_dubbing,
        inputs=[video_input, target_language, speaker_toggle],
        outputs=status_output
    )

if __name__ == "__main__":
    demo.launch(inbrowser=True)
