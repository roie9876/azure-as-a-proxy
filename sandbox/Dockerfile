# syntax=docker/dockerfile:1.7
# Sandbox image: Kasm Chromium with header/fingerprint normalization.
#
# IMPORTANT: kasmweb/chromium runs Chromium in an isolated user; we layer:
#   1. A Chromium policy file forcing UA, lang, WebRTC, DoH off.
#   2. An entrypoint that exports CHROME_USER_AGENT etc. and launches Kasm.
#
# NOTE: For production, mirror this image into your own ACR and pin the digest.
ARG KASM_TAG=1.16.0
FROM kasmweb/chromium:${KASM_TAG}

USER root

# Chromium managed policies — applied to every browser launch in this image.
COPY chromium-policies.json /etc/chromium/policies/managed/cloak.json

# Entrypoint shim that prepends fingerprint normalization flags.
COPY entrypoint.sh /usr/local/bin/cloak-entrypoint
RUN chmod +x /usr/local/bin/cloak-entrypoint

# Drop back to the kasm user the upstream image expects.
# (Kasm runs as uid 1000; check the upstream for changes.)
USER 1000

EXPOSE 6901
ENTRYPOINT ["/usr/local/bin/cloak-entrypoint"]
