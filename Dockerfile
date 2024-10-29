FROM python:3-slim

WORKDIR /pvforecast
COPY . .

RUN pip install --no-cache-dir -r requirements.txt && rm -rfv /root/.cache/pip

RUN : \
	&& apt-get update && apt-get install -y --no-install-recommends cron \
	&& apt-get -qy clean \
	&& rm -rfv /var/lib/apt \
	&& rm -rfv /var/lib/dpkg

RUN touch /var/log/cron.log && \
	echo "*/1 * * * * cd /pvforecast && /usr/local/bin/python PVForecasts.py >> /var/log/cron.log 2>&1" | crontab -

CMD cron && exec tail -f /var/log/cron.log

