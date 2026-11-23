# Developer Edition edition manager: installs the edition chosen at sign-up (see manager.py).
FROM python:3.12-alpine
RUN apk add --no-cache docker-cli docker-cli-compose docker-cli-buildx
COPY manager.py /manager.py
CMD ["python3", "-u", "/manager.py"]
