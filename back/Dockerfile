FROM golang:1.25-alpine AS builder

WORKDIR /app

# Установка зависимостей
COPY go.mod go.sum ./
RUN go mod download

# Копирование исходников
COPY . .

# Сборка
RUN CGO_ENABLED=0 GOOS=linux go build -o server ./cmd/server

# Финальный образ
FROM alpine:latest

RUN apk --no-cache add ca-certificates

WORKDIR /root/

COPY --from=builder /app/server .
COPY --from=builder /app/.env .env

EXPOSE 8080

CMD ["./server"]