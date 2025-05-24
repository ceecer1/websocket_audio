import asyncio
import websockets
import wave
import datetime
import os
import struct  # For converting bytes to numbers
import math    # For RMS calculation (optional, max amplitude is simpler)

# Audio settings from client (ensure these match what the client sends)
SAMPLE_RATE = 16000  # Client sends 16kHz
CHANNELS = 1         # Client sends mono
SAMPLE_WIDTH = 2     # Client sends 16-bit PCM (2 bytes per sample)
BYTES_PER_SAMPLE = CHANNELS * SAMPLE_WIDTH
BYTES_PER_SECOND = SAMPLE_RATE * BYTES_PER_SAMPLE

# Silence Detection Parameters
SILENCE_THRESHOLD = 800  # Amplitude threshold for 16-bit audio (-32768 to 32767). Tune this!
                          # Lower means more sensitive to noise (less likely to detect silence).
                          # Higher means less sensitive (more likely to call faint speech silence).
MAX_SILENCE_SECONDS = 1.0 # Stop relaying if silent for this duration.

CONNECTIONS = set()
OUTPUT_DIR = "received_audio"

os.makedirs(OUTPUT_DIR, exist_ok=True)

def is_chunk_silent_max_abs(pcm_chunk_bytes, threshold):
    """
    Checks if a PCM chunk is silent based on maximum absolute amplitude.
    Assumes 16-bit signed PCM.
    """
    if not pcm_chunk_bytes:
        return True # Empty chunk is silent

    num_samples = len(pcm_chunk_bytes) // SAMPLE_WIDTH
    max_amplitude = 0
    for i in range(num_samples):
        sample_bytes = pcm_chunk_bytes[i*SAMPLE_WIDTH : (i+1)*SAMPLE_WIDTH]
        # Assuming little-endian format, which is common for WAV and many clients
        sample_val = struct.unpack('<h', sample_bytes)[0]
        if abs(sample_val) > max_amplitude:
            max_amplitude = abs(sample_val)
    
    # print(f"Max amplitude in chunk: {max_amplitude}") # For debugging threshold
    return max_amplitude < threshold

# Optional: RMS based silence detection (more robust but slightly more complex)
def is_chunk_silent_rms(pcm_chunk_bytes, threshold_rms_normalized):
    """
    Checks if a PCM chunk is silent based on Root Mean Square (RMS) amplitude.
    Assumes 16-bit signed PCM.
    threshold_rms_normalized should be a small float (e.g., 0.01 for 1% of max possible RMS).
    """
    if not pcm_chunk_bytes:
        return True

    num_samples = len(pcm_chunk_bytes) // SAMPLE_WIDTH
    if num_samples == 0:
        return True
        
    sum_squares = 0
    for i in range(num_samples):
        sample_bytes = pcm_chunk_bytes[i*SAMPLE_WIDTH : (i+1)*SAMPLE_WIDTH]
        sample_val = struct.unpack('<h', sample_bytes)[0]
        # Normalize sample to -1.0 to 1.0 range for consistent RMS calculation
        normalized_sample = sample_val / 32767.0 
        sum_squares += normalized_sample * normalized_sample
    
    rms = math.sqrt(sum_squares / num_samples)
    # print(f"RMS in chunk: {rms:.4f}") # For debugging threshold
    return rms < threshold_rms_normalized


async def audio_handler(websocket):
    client_address = websocket.remote_address
    sane_client_address = str(client_address).replace(":", "_").replace("(", "").replace(")", "").replace(",", "")
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = os.path.join(OUTPUT_DIR, f"audio_{sane_client_address}_{timestamp}.wav")
    
    print(f"Client connected: {client_address}. Will save audio to {filename}")
    CONNECTIONS.add(websocket)
    
    pcm_data_chunks = [] # For saving the entire session to WAV

    # Silence detection state variables for this connection
    current_silence_accumulated_duration = 0.0
    relaying_audio = True # Start by relaying audio

    try:
        async for message in websocket:
            if isinstance(message, bytes):
                # ALWAYS store the received PCM data chunk for WAV file saving
                pcm_data_chunks.append(message)
                
                chunk_duration_seconds = len(message) / BYTES_PER_SECOND

                # --- Silence Detection Logic ---
                # Use is_chunk_silent_max_abs for simplicity or is_chunk_silent_rms (with adjusted threshold)
                is_silent_now = is_chunk_silent_max_abs(message, SILENCE_THRESHOLD)
                # Example for RMS: is_silent_now = is_chunk_silent_rms(message, 0.005) # Tune 0.005

                if is_silent_now:
                    current_silence_accumulated_duration += chunk_duration_seconds
                    if current_silence_accumulated_duration >= MAX_SILENCE_SECONDS:
                        if relaying_audio:
                            print(f"[{client_address}] Silence duration ({current_silence_accumulated_duration:.2f}s) exceeded threshold. Pausing relay.")
                            relaying_audio = False
                            # Optionally, send a control message to client:
                            # await websocket.send("SILENCE_DETECTED_PAUSE_RELAY")
                else: # Speech detected (not silent)
                    if not relaying_audio:
                        print(f"[{client_address}] Speech detected after silence. Resuming relay.")
                        relaying_audio = True
                        # Optionally, send a control message to client:
                        # await websocket.send("SPEECH_DETECTED_RESUME_RELAY")
                    current_silence_accumulated_duration = 0.0 # Reset silence counter

                # --- Relaying Logic ---
                if relaying_audio:
                    # print(f"Relaying {len(message)} audio bytes from {client_address}")
                    await websocket.send(message)
                # else:
                    # print(f"[{client_address}] Not relaying silent chunk (or still in silent period).")
                
            else: # Text message
                print(f"Received text message from {client_address}: {message}")
                # Echo back text messages if needed
                # await websocket.send(f"Server received your text: {message}")

    except websockets.ConnectionClosedOK:
        print(f"Client {client_address} disconnected normally.")
    except websockets.ConnectionClosedError as e:
        print(f"Client {client_address} disconnected with error: {e}")
    except Exception as e:
        print(f"An error occurred with {client_address}: {e}")
    finally:
        if websocket in CONNECTIONS:
            CONNECTIONS.remove(websocket)
        print(f"Client {client_address} removed from connections.")

        if pcm_data_chunks:
            print(f"Writing {len(pcm_data_chunks)} chunks of audio data to {filename}...")
            try:
                with wave.open(filename, 'wb') as wf:
                    wf.setnchannels(CHANNELS)
                    wf.setsampwidth(SAMPLE_WIDTH)
                    wf.setframerate(SAMPLE_RATE)
                    wf.writeframes(b''.join(pcm_data_chunks))
                print(f"Successfully saved audio to {filename}")
            except Exception as e_wave:
                print(f"Error writing WAV file {filename}: {e_wave}")
        else:
            print(f"No audio data received from {client_address} to save.")


async def main():
    host = "0.0.0.0"
    port = 8080
    print(f"WebSocket audio server starting on ws://{host}:{port}")
    print(f"Silence threshold (max_abs): {SILENCE_THRESHOLD}, Max silence duration: {MAX_SILENCE_SECONDS}s")
    print(f"Received audio will be saved in '{os.path.abspath(OUTPUT_DIR)}' directory.")
    
    async with websockets.serve(audio_handler, host, port, max_size=None): # max_size=None for large audio data if needed
        await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer shutting down.")