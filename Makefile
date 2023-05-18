clean:
	isort ./src
	black ./src

typecheck:
	mypy ./src

ssl_certs:
	cd ./ssl_certs; \
	./create_certs.sh ca && \
	./create_certs.sh issue client && \
	./create_certs.sh issue server
