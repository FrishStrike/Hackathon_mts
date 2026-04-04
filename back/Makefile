.PHONY: run build docker-up docker-down clean

run:
	go run cmd/server/main.go

build:
	go build -o bin/server cmd/server/main.go

docker-up:
	docker-compose up -d

docker-down:
	docker-compose down

clean:
	rm -rf bin/
	go clean

test:
	go test -v ./...

deps:
	go mod download
	go mod tidy