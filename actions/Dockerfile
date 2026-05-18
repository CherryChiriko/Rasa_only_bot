FROM rasa/rasa-sdk:3.6.2
WORKDIR /app
USER root
RUN pip install requests
COPY . /app/actions
USER 1001