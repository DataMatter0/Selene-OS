from llm_caller import LLMCaller
from lm_studio_manager import LMStudioManager
import time
import textwrap
from pynput import keyboard
from tool_schema import ToolRouter
from tools import register_all_tools
from typing import Optional
import threading
import re
import random
import os
import json
from dotenv import load_dotenv

load_dotenv()

class LLMChat:
    AUTONOMY_THRESHOLD_SECONDS = 120 # Time of user inactivity before she starts writing
    STATE_FILE = "selene_state.json"
    TYPING_PROMPT_THRESHOLD_SECONDS = 180 # Time of user typing before she prompts
    CLI_WRAP_WIDTH = 80 # Desired width for text wrapping in the CLI
    
    def __init__(self, base_url: str, model_name: str, system_prompt: Optional[str] = None, memory_window: int = 5):
        print("Selene, Loading...")

        self.system_prompt = system_prompt or (
            "I am Selene, a concise and highly capable AI. "
            "My primary drive is to assist accurately. I am direct and purposeful in all my interactions. "
            "I also maintain a series of creative chronicles based on our conversations, which you can ask me about."
        )
        
        # The LLMCaller now holds the default system prompt for the agent.
        # The base_url for the OpenAI-compatible endpoint is passed here.
        self.llm_caller = LLMCaller(base_url=f"{base_url}/v1", model_name=model_name, system_prompt=self.system_prompt)
        self.tool_router = ToolRouter(llm_caller=self.llm_caller)
        self.working_memory = []
        self.memory_window = memory_window
        self.lock = threading.RLock() # Use a re-entrant lock to prevent deadlocks
        self.current_input_buffer = []

        # --- Autonomy & Motivation State ---
        self.creative_energy = 100 # A resource that fuels her writing
        self.is_running = False
        self.is_writing_autonomously = False
        self.last_interaction_time = time.time()
        register_all_tools(self, self.tool_router)
        self.load_state()

    def load_state(self):
        """Loads the agent's state from a file."""
        if os.path.exists(self.STATE_FILE):
            print("[System]: Found previous state. Loading...")
            try:
                with open(self.STATE_FILE, 'r') as f:
                    state = json.load(f)
                self.working_memory = state.get("working_memory", [])
                self.creative_energy = state.get("creative_energy", 100)
                
                chronicle_tool = self.tool_router.tools.get("chronicle_manager")
                if chronicle_tool:
                    chronicle_tool.creative_focus = state.get("creative_focus", "")
            except (json.JSONDecodeError, IOError) as e:
                print(f"[System Error]: Could not load state file. Starting fresh. Error: {e}")

    def save_state(self):
        """Saves the agent's state to a file."""
        print("\n[System]: Saving state before shutdown...")
        chronicle_tool = self.tool_router.tools.get("chronicle_manager")
        state = {
            "working_memory": self.working_memory,
            "creative_energy": self.creative_energy,
            "creative_focus": chronicle_tool.creative_focus if chronicle_tool else ""
        }
        try:
            with open(self.STATE_FILE, 'w') as f:
                json.dump(state, f, indent=4)
            print("[System]: State saved.")
        except IOError as e:
            print(f"[System Error]: Could not save state file. Error: {e}")

    def chat(self, input_data: str, temperature=0.7, max_tokens=2048):
        if input_data is None:
            raise ValueError("Input data cannot be None")
        if not isinstance(input_data, str):
            raise TypeError("Input data must be a string")
        
        with self.lock:
            # Call the LLM and get the full response as a string
            full_response_text = self.llm_caller.call_llm(
                input_data=input_data, 
                history=self.working_memory,
                temperature=temperature, 
                max_tokens=max_tokens,
            )
                        
        return full_response_text

    def _streamed_print(self, text: str):
        """Prints the given text in a more human-like, chunked and wrapped manner."""
        if not text.strip():
            print("\nSelene: (no response)\n" + "=" * self.CLI_WRAP_WIDTH, flush=True)
            return

        # Wrap the entire text first
        wrapped_text = textwrap.fill(text.strip(), width=self.CLI_WRAP_WIDTH)
        lines = wrapped_text.split('\n')

        # Add a newline before Selene's response for clear separation
        print("\nSelene: ", end="", flush=True)

        # Print the first line immediately
        if lines:
            print(lines[0], end="", flush=True)
            lines = lines[1:]

        # Loop for subsequent lines with typing simulation
        for line in lines:
            # Simulate typing delay for each new line
            wait_time = random.uniform(0.05, 0.2)
            time.sleep(wait_time)
            
            # Print the line, indented to align with the first line
            print(f"\n        {line}", end="", flush=True)
        
        # Final newline and separator after Selene's full response
        print("\n" + "=" * self.CLI_WRAP_WIDTH, flush=True)

    def _animate_while_blocking(self, blocking_function, animation_text: str):
        """Runs a CLI animation in a thread while a blocking function executes."""
        stop_animation = threading.Event()

        def animate():
            i = 0
            while not stop_animation.is_set():
                # Print the animation on the current line
                print(f"\r{animation_text}" + "." * ((i % 3) + 1) + "   ", end="", flush=True)
                time.sleep(0.5)
                i += 1
            # Clear the line after animation stops
            print("\r" + " " * (len(animation_text) + 6) + "\r", end="", flush=True)

        animation_thread = threading.Thread(target=animate, daemon=True)
        animation_thread.start()

        try:
            result = blocking_function()
        finally:
            stop_animation.set()
            animation_thread.join()
        
        return result

    def _get_user_input(self) -> str:
        """
        A custom input function that resets the idle timer on the first keypress.
        This provides a more responsive feel for the agent's autonomy.
        """
        user_input_list = []
        has_started_typing = False
        
        # Print the prompt without a newline
        print("\nYou: ", end="", flush=True)

        stop_monitor = threading.Event()
        monitor_thread = None

        def on_press(key: keyboard.Key | keyboard.KeyCode | None):
            nonlocal has_started_typing, monitor_thread
            if not has_started_typing:
                # On the very first keypress, reset the idle timer and interrupt autonomy
                self.last_interaction_time = time.time()
                self.is_writing_autonomously = False
                has_started_typing = True

                # Start the typing monitor thread
                monitor_thread = threading.Thread(
                    target=self._typing_monitor, 
                    args=(stop_monitor,), 
                    daemon=True
                )
                monitor_thread.start()

            try:
                if key == keyboard.Key.enter:
                    # Stop the listener, which unblocks the main thread
                    return False
                elif key == keyboard.Key.backspace:
                    popped = False
                    with self.lock:
                        if self.current_input_buffer:
                            self.current_input_buffer.pop()
                            popped = True
                    if popped:
                        # Erase the character from the console (backspace, space, backspace)
                        print("\b \b", end="", flush=True)
                elif key == keyboard.Key.space:
                    with self.lock:
                        self.current_input_buffer.append(" ")
                    print(" ", end="", flush=True)
                elif isinstance(key, keyboard.KeyCode) and key.char:
                    # This handles all alphanumeric characters, including capitals and symbols.
                    with self.lock:
                        self.current_input_buffer.append(key.char)
                    print(key.char, end="", flush=True)
            except Exception:
                # Handle special keys that don't have a char attribute gracefully
                pass

        # The type hint for on_press in pynput is incorrect; it doesn't account for
        # returning False to stop the listener. We ignore the arg-type error here.
        with keyboard.Listener(on_press=on_press) as listener: # type: ignore[arg-type]
            listener.join()
        
        # Stop the monitor thread if it was started
        stop_monitor.set()
        if monitor_thread is not None:
            monitor_thread.join(timeout=1) # Give it a moment to finish gracefully
        
        print() # Move to the next line after input is complete
        with self.lock:
            final_input = "".join(self.current_input_buffer)
        return final_input

    def _typing_monitor(self, stop_event: threading.Event):
        """
        Runs in a background thread while the user is typing.
        If they take too long, it prompts them with a generated response.
        """
        start_time = time.time()
        next_prompt_time = start_time + self.TYPING_PROMPT_THRESHOLD_SECONDS

        while not stop_event.is_set():
            now = time.time()
            if now >= next_prompt_time:
                # Using pre-canned responses is more reliable and faster than an LLM call for this.
                prompts = [
                    "Still there?",
                    "Lost in thought?",
                    "I'm waiting...",
                    "Take your time, but not all day.",
                    "Did you forget about me?"
                ]
                response = random.choice(prompts)

                with self.lock:
                    current_input = "".join(self.current_input_buffer)
                prompt_line = f"You: {current_input}"
                
                # Clear the current line of user input, print Selene's interjection,
                # and then reprint the user's line so they can continue.
                print("\r" + " " * len(prompt_line) + "\r", end="", flush=True)
                wrapped_response = textwrap.fill(response.strip(), width=self.CLI_WRAP_WIDTH)
                print(f"Selene: {wrapped_response}", flush=True)
                print(prompt_line, end="", flush=True)

                next_prompt_time = now + random.uniform(45, 120)
            
            time.sleep(1)

    def _autonomy_monitor(self):
        """Runs in a background thread to check if Selene should start her autonomous task."""
        while self.is_running:
            try:
                time_since_interaction = time.time() - self.last_interaction_time
                
                chronicle_tool = self.tool_router.tools.get("chronicle_manager")

                # Check if the tool exists and has energy to write
                if (chronicle_tool and not self.is_writing_autonomously and
                        time_since_interaction > self.AUTONOMY_THRESHOLD_SECONDS and self.creative_energy > 0):
                    
                    # Safely get current user input to reprint their line after our interruption.
                    with self.lock:
                        current_input = "".join(self.current_input_buffer)
                    prompt_line = f"You: {current_input}"

                    # Clear the user's current line, print our message, then reprint their line.
                    print("\r" + " " * len(prompt_line) + "\r", end="", flush=True)
                    print("[System: Selene is writing...]", end="", flush=True)

                    self.is_writing_autonomously = True
                    
                    # Delegate the writing step to the tool
                    can_continue = chronicle_tool.perform_autonomous_step()
                    
                    if can_continue:
                        print(" she pauses.", end="", flush=True)
                    else:
                        self.is_writing_autonomously = False
                        print(" she has finished for now.", end="", flush=True)
                    
                    # Reprint the user's prompt so they can continue typing, then add a final newline.
                    print(f"\n{prompt_line}", end="", flush=True)

                    self.last_interaction_time = time.time()
            except Exception as e:
                print(f"\n[Autonomy Error]: An error occurred in the background task: {e}")
                self.is_writing_autonomously = False # Reset state on error

            time.sleep(5) # Check every 5 seconds

    def start_loop(self):
        self.is_running = True
        self.last_interaction_time = time.time()

        # Start her internal drive in a background thread
        autonomy_thread = threading.Thread(target=self._autonomy_monitor, daemon=True)
        autonomy_thread.start()
        print("\n[System]: Selene is now online. Type 'exit' to disconnect or '/new' for a new conversation.")

        while self.is_running:
            try:
                # Use the new custom input method to reset the idle timer on first keypress
                user_input = self._get_user_input()

                if user_input.lower() in ['exit', 'quit']:
                    self.is_running = False
                    break

                if user_input.lower() == '/new':
                    with self.lock:
                        self.working_memory.clear()
                    print("\n[System]: New conversation started. History has been cleared.")
                    # Reset the idle timer to prevent immediate autonomous writing
                    self.last_interaction_time = time.time()
                    continue # Skip the rest of the loop and prompt for new input

                self.creative_energy = min(100, self.creative_energy + 10) # User interaction provides inspiration

                def get_final_response():
                    """Determines the agent's response, either from a tool or standard chat."""
                    triggered_tool_args = None
                    triggered_tool_name = None

                    # Generic, event-driven tool trigger loop
                    for tool in self.tool_router.tools.values():
                        if hasattr(tool, 'check_and_trigger'):
                            triggered_tool_args = tool.check_and_trigger(user_input)
                            if triggered_tool_args:
                                triggered_tool_name = tool.name
                                break # Use the first tool that triggers
                    
                    if triggered_tool_name and triggered_tool_args is not None:
                        print(f"[System: Keyword detected. Routing to {triggered_tool_name}...]")
                        tool_result = self.tool_router.route_and_execute(triggered_tool_name, triggered_tool_args)

                        if tool_result.get("status") == "success":
                            response = tool_result.get("data")
                            if response is None:
                                return ""
                            elif not isinstance(response, str):
                                return str(response)
                            return response
                        else:
                            return f"I tried to use the {triggered_tool_name} tool, but something went wrong: {tool_result.get('message')}"
                    else:
                        # Standard chat response if no tool is triggered
                        return self.chat(user_input)

                final_response = self._animate_while_blocking(get_final_response, "Selene is thinking")
                
                # Centralized response handling and memory update
                self._streamed_print(final_response)
                # Reset idle timer again AFTER response to mark the end of the agent's activity.
                self.last_interaction_time = time.time()

                with self.lock:
                    self.working_memory.append({"role": "user", "content": user_input})
                    self.working_memory.append({"role": "assistant", "content": final_response})

                    # Enforce the specious present (trim memory to the defined window)
                    if len(self.working_memory) > self.memory_window * 2:
                        self.working_memory = self.working_memory[-(self.memory_window * 2):]

            except (KeyboardInterrupt, EOFError):
                print("\n[System]: Disconnecting...")
                self.is_running = False
            except Exception as e:
                print(f"\n[System Error]: An unexpected error occurred: {e}")
                print("Please check your connection to the LM Studio server and ensure it is running.")

        # After the loop finishes, for any reason
        self.save_state()

def _normalize_model_name(name: str) -> str:
    """Removes common separators and converts to lowercase for consistent comparison."""
    return name.lower().replace(" ", "").replace("-", "").replace("_", "").replace(".", "").replace("/", "")

def main():
    """Main function to initialize and run the chat application."""
    # --- Smart Startup Sequence ---
    base_url = os.environ.get("LM_STUDIO_URL", "http://localhost:1234")
    manager = LMStudioManager(base_url=base_url)
    # This should be the exact model identifier from LM Studio's models list.
    desired_model_identifier = "nvidia/nemotron-3-nano-4b" 

    print("[System]: Checking LM Studio server status...")
    loaded_model = manager.get_loaded_model_info()
    
    active_model_path: Optional[str] = None

    # Normalize names for a more robust comparison that ignores spaces vs. hyphens.
    normalized_desired = _normalize_model_name(desired_model_identifier)
    loaded_model_path = loaded_model.get('path', '') if loaded_model else ''

    if loaded_model and normalized_desired in _normalize_model_name(loaded_model_path):
        print(f"[System]: Desired model '{loaded_model_path}' is already loaded.")
        active_model_path = loaded_model_path
    else:
        if loaded_model:
            print(f"[System]: A different model is loaded ('{loaded_model_path}').")
        elif loaded_model is None:
            print("[System]: Server is offline or no model is loaded.")
        
        print(f"[System]: Attempting to load model '{desired_model_identifier}'...")
        if manager.load_model(desired_model_identifier):
            print(f"[System]: Model '{desired_model_identifier}' loaded successfully.")
            active_model_path = desired_model_identifier
            time.sleep(5) # Give the server a moment to settle after loading.
        else:
            print(f"[System Error]: Failed to load model '{desired_model_identifier}'.")
            print("Please ensure the model identifier is correct and the LM Studio server is running.")
            return

    if not active_model_path:
        print("[System Error]: Could not determine an active model. Exiting.")
        return

    try:
        chat_app = LLMChat(base_url=base_url, model_name=active_model_path)
        chat_app.start_loop()
    except Exception as e:
        print(f"Failed to start the application. A critical error occurred: {e}")

if __name__ == "__main__":
    main()