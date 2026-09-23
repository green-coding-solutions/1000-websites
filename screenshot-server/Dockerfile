# server.py is standard library only, so the image is the stock Python image
# plus that one file. It is copied in rather than bind-mounted because hosts
# like Coolify run the compose file from their own directory, where a relative
# ./server.py does not exist and Docker mounts an empty directory in its place.
FROM python:3.13-slim
COPY server.py /app/server.py
