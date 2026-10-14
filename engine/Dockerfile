# syntax=docker/dockerfile:1.7
# Static Tesseract OCR engine for Linux.
#
# scripts/engine.py build supplies every argument from engine/source-lock.json;
# each download is checked against its pinned digest. The engine decodes no
# image format itself: its consumer hands it an 8-bit PGM on stdin, so
# Leptonica is built without any codec and the executable links only
# Tesseract, Leptonica, musl, and the GCC runtime libraries, all statically.
ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS build
ARG BASE_IMAGE
ARG TARGETARCH
ARG ENGINE_VERSION
ARG SOURCE_LOCK_SHA256
ARG TESSERACT_VERSION
ARG TESSERACT_URL
ARG TESSERACT_SHA256
ARG LEPTONICA_VERSION
ARG LEPTONICA_URL
ARG LEPTONICA_SHA256
ARG TESSDATA_FAST_COMMIT
ARG ENG_SHA256
ARG OSD_SHA256
ARG MUSL_COPYRIGHT_URL
ARG MUSL_COPYRIGHT_SHA256
ARG GPL3_URL
ARG GPL3_SHA256
ARG GCC_EXCEPTION_URL
ARG GCC_EXCEPTION_SHA256
ARG SOURCE_DATE_EPOCH

RUN apk add --no-cache build-base autoconf automake libtool pkgconf curl tar gzip

WORKDIR /src

RUN curl -fsSL -o leptonica.tar.gz "${LEPTONICA_URL}" \
 && echo "${LEPTONICA_SHA256}  leptonica.tar.gz" | sha256sum -c - \
 && tar -xzf leptonica.tar.gz && rm leptonica.tar.gz
RUN cd "leptonica-${LEPTONICA_VERSION}" \
 && ./configure --prefix=/opt/lept --disable-shared --enable-static --disable-programs \
      --without-zlib --without-libpng --without-jpeg --without-giflib --without-libtiff \
      --without-libwebp --without-libwebpmux --without-libopenjpeg \
      CFLAGS="-O2 -ffile-prefix-map=/src=." \
 && make -j"$(nproc)" && make install

RUN curl -fsSL -o tesseract.tar.gz "${TESSERACT_URL}" \
 && echo "${TESSERACT_SHA256}  tesseract.tar.gz" | sha256sum -c - \
 && tar -xzf tesseract.tar.gz && rm tesseract.tar.gz
# The legacy recognizer stays in: orientation detection (osd) runs on it.
RUN cd "tesseract-${TESSERACT_VERSION}" \
 && ./autogen.sh \
 && PKG_CONFIG_PATH=/opt/lept/lib/pkgconfig ./configure --prefix=/opt/tess \
      --disable-shared --enable-static --disable-openmp --disable-graphics --disable-doc \
      --without-archive --without-curl \
      CXXFLAGS="-O2 -ffile-prefix-map=/src=." \
 && make -j"$(nproc)" LDFLAGS="-all-static" && make install

COPY source-lock.json licensing/NOTICES-tesseract.md /in/
RUN set -eu; \
    echo "${SOURCE_LOCK_SHA256}  /in/source-lock.json" | sha256sum -c -; \
    mkdir -p /out/ocr/bin /out/ocr/tessdata /out/ocr/licenses; \
    cp /opt/tess/bin/tesseract /out/ocr/bin/tesseract; \
    strip /out/ocr/bin/tesseract; \
    cd /out/ocr/tessdata; \
    for name in eng osd; do \
      curl -fsSL -o "${name}.traineddata" \
        "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/${TESSDATA_FAST_COMMIT}/${name}.traineddata"; \
    done; \
    echo "${ENG_SHA256}  eng.traineddata" | sha256sum -c -; \
    echo "${OSD_SHA256}  osd.traineddata" | sha256sum -c -; \
    cd /out/ocr/licenses; \
    cp "/src/tesseract-${TESSERACT_VERSION}/LICENSE" LICENSE-tesseract; \
    cp "/src/leptonica-${LEPTONICA_VERSION}/leptonica-license.txt" LICENSE-leptonica; \
    curl -fsSL -o COPYRIGHT-musl "${MUSL_COPYRIGHT_URL}"; \
    echo "${MUSL_COPYRIGHT_SHA256}  COPYRIGHT-musl" | sha256sum -c -; \
    curl -fsSL -o GPL-3.0 "${GPL3_URL}"; \
    echo "${GPL3_SHA256}  GPL-3.0" | sha256sum -c -; \
    curl -fsSL -o GCC-RUNTIME-LIBRARY-EXCEPTION-3.1 "${GCC_EXCEPTION_URL}"; \
    echo "${GCC_EXCEPTION_SHA256}  GCC-RUNTIME-LIBRARY-EXCEPTION-3.1" | sha256sum -c -; \
    cp /in/source-lock.json /in/NOTICES-tesseract.md /out/ocr/

# provenance.json records what went into the archive, including the Alpine
# packages the toolchain came from, which the base image digest does not fix.
RUN set -eu; cd /out/ocr; \
    BIN_SHA="$(sha256sum bin/tesseract | cut -d' ' -f1)"; \
    BIN_BYTES="$(stat -c %s bin/tesseract)"; \
    APK="$(apk info -v 2>/dev/null | sort | tr '\n' ' ' | sed 's/ $//')"; \
    GCC_PKG="$(apk info -v 2>/dev/null | grep '^gcc-' | head -1)"; \
    printf '%s\n' \
      '{' \
      "  \"engine_version\": \"${ENGINE_VERSION}\"," \
      "  \"platform\": \"linux-${TARGETARCH}\"," \
      "  \"source_lock_sha256\": \"${SOURCE_LOCK_SHA256}\"," \
      "  \"tesseract\": {\"version\": \"${TESSERACT_VERSION}\", \"sha256\": \"${TESSERACT_SHA256}\"}," \
      "  \"leptonica\": {\"version\": \"${LEPTONICA_VERSION}\", \"sha256\": \"${LEPTONICA_SHA256}\"}," \
      "  \"tessdata_fast\": {\"commit\": \"${TESSDATA_FAST_COMMIT}\"}," \
      "  \"gcc_runtime\": {\"package\": \"${GCC_PKG}\"}," \
      "  \"build\": {\"base_image\": \"${BASE_IMAGE}\", \"packages\": \"${APK}\", \"source_date_epoch\": ${SOURCE_DATE_EPOCH}}," \
      "  \"binary\": {\"sha256\": \"${BIN_SHA}\", \"bytes\": ${BIN_BYTES}}" \
      '}' > provenance.json; \
    printf '%s\n' "${ENGINE_VERSION}" > VERSION

RUN set -eu; cd /out; \
    NAME="paintedwolf-tesseract-${ENGINE_VERSION}-linux-${TARGETARCH}"; \
    find ocr -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +; \
    tar --sort=name --mtime="@${SOURCE_DATE_EPOCH}" --owner=0 --group=0 --numeric-owner -cf - ocr \
      | gzip -n -9 > "/out/${NAME}.tar.gz"

FROM scratch AS artifact
COPY --from=build /out/*.tar.gz /
