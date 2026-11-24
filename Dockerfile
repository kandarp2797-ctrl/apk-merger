# Builds a container with the whole toolchain so you never install anything locally.
# A cloud host builds this once and gives you a URL.
FROM eclipse-temurin:17-jdk

# system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip unzip wget ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# apktool
RUN wget -q -O /usr/local/bin/apktool.jar \
        https://bitbucket.org/iBotPeaches/apktool/downloads/apktool_2.9.3.jar && \
    printf '#!/bin/sh\nexec java -jar /usr/local/bin/apktool.jar "$@"\n' \
        > /usr/local/bin/apktool && \
    chmod +x /usr/local/bin/apktool

# Android build-tools (zipalign + apksigner) via the command-line SDK tools
ENV ANDROID_SDK_ROOT=/opt/android-sdk
RUN mkdir -p $ANDROID_SDK_ROOT/cmdline-tools && \
    wget -q -O /tmp/cmdtools.zip \
        https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip && \
    unzip -q /tmp/cmdtools.zip -d $ANDROID_SDK_ROOT/cmdline-tools && \
    mv $ANDROID_SDK_ROOT/cmdline-tools/cmdline-tools \
       $ANDROID_SDK_ROOT/cmdline-tools/latest && \
    yes | $ANDROID_SDK_ROOT/cmdline-tools/latest/bin/sdkmanager --licenses >/dev/null && \
    $ANDROID_SDK_ROOT/cmdline-tools/latest/bin/sdkmanager \
        "build-tools;34.0.0" "platform-tools" >/dev/null && \
    rm /tmp/cmdtools.zip
ENV PATH="$PATH:$ANDROID_SDK_ROOT/build-tools/34.0.0:$ANDROID_SDK_ROOT/platform-tools"

WORKDIR /app
COPY . /app
RUN python3 -m pip install --no-cache-dir --break-system-packages \
        fastapi "uvicorn[standard]" python-multipart

# Hosts set $PORT; fall back to 8000 for local `docker run`.
EXPOSE 8000
CMD ["sh", "-c", "python3 -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
