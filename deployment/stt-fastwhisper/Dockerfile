# Matches the CUDA runtime the existing speaches service already runs
# successfully on this host (12.6.3). Host driver (CUDA 13.2) covers it via
# backward compatibility. -cudnn-runtime- provides cuBLAS + cuDNN 9 that
# ctranslate2 needs; PyAV wheels bundle their own libav, so no system ffmpeg.
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04

# HF_HOME under /home/ubuntu (not /root) to match stt-whisper — /root previously
# caused docker cache-access problems on this host. Compose overrides this too.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/home/ubuntu/.cache/huggingface

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# ubuntu24.04 ships an externally-managed Python (PEP 668); inside a container
# installing into the system interpreter is fine.
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY main.py .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
