# Security notes

The API talks to the Docker daemon in order to create isolated The Isle containers. Docker socket access is privileged by design. Keep the panel on a trusted network, use a TLS reverse proxy or VPN for remote use, and grant the Owner role only to trusted administrators.

Report security concerns privately to the project maintainer rather than opening public issues with exploit details.
