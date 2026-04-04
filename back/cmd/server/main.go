package main

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/go-chi/cors"
	"github.com/joho/godotenv"

	"mts/internal/api"
	"mts/internal/sse"
	"mts/internal/storage"
	"mts/internal/worker"
)

func main() {
	// Загрузка .env
	if err := godotenv.Load(); err != nil {
		log.Println("Warning: .env file not found, using environment variables")
	}

	// Инициализация PostgreSQL
	pg, err := storage.NewPostgresStorage(
		getEnv("POSTGRES_HOST", "localhost"),
		getEnv("POSTGRES_PORT", "5432"),
		getEnv("POSTGRES_USER", "postgres"),
		getEnv("POSTGRES_PASSWORD", "pass"),
		getEnv("POSTGRES_DB", "hack"),
	)
	if err != nil {
		log.Fatalf("Failed to init PostgreSQL: %v", err)
	}
	defer pg.Close()

	// Инициализация Redis
	rd, err := storage.NewRedisStorage(
		getEnv("REDIS_HOST", "localhost"),
		getEnv("REDIS_PORT", "6379"),
		getEnv("REDIS_PASSWORD", ""),
		10, // TTL 10 минут
	)
	if err != nil {
		log.Fatalf("Failed to init Redis: %v", err)
	}
	defer rd.Close()

	// SSE брокер
	sseBroker := sse.NewBroker()
	defer sseBroker.Close()

	// Воркер для обработки задач
	mlURL := getEnv("ML_SERVICE_URL", "http://localhost:8001")
	mlTimeout := getEnvAsInt("ML_TIMEOUT", 30)

	w := worker.NewWorker(
		rd,
		pg,
		sseBroker,
		mlURL,
		time.Duration(mlTimeout)*time.Second,
	)

	// Запуск воркера в горутине
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go w.Start(ctx)
	defer w.Stop()

	// HTTP handlers
	handlers := api.NewHandlers(pg, rd, sseBroker)

	// Router
	r := chi.NewRouter()

	// Middleware
	r.Use(middleware.Logger)
	r.Use(middleware.Recoverer)
	r.Use(middleware.Timeout(60 * time.Second))
	r.Use(cors.Handler(cors.Options{
		AllowedOrigins:   []string{"*"},
		AllowedMethods:   []string{"GET", "POST", "PUT", "DELETE", "OPTIONS"},
		AllowedHeaders:   []string{"Accept", "Authorization", "Content-Type", "X-CSRF-Token"},
		ExposedHeaders:   []string{"Link"},
		AllowCredentials: false,
		MaxAge:           300,
	}))

	// Routes
	r.Get("/api/health", handlers.HandleHealth)
	r.Post("/api/query", handlers.HandleQuery)
	r.Get("/api/result/{id}", handlers.HandleResult)
	r.Get("/api/stream/{id}", handlers.HandleSSE)
	r.Get("/api/history", handlers.HandleHistory)
	r.Post("/internal/trace", handlers.HandleInternalTrace)

	// HTTP server
	port := getEnv("PORT", "8080")
	srv := &http.Server{
		Addr:         ":" + port,
		Handler:      r,
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	// Graceful shutdown
	go func() {
		log.Printf("Server starting on port %s", port)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("Server failed: %v", err)
		}
	}()

	// Wait for interrupt signal
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit

	log.Println("Shutting down server...")

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer shutdownCancel()

	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Fatalf("Server forced to shutdown: %v", err)
	}

	log.Println("Server exited")
}

func getEnv(key, defaultValue string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return defaultValue
}

func getEnvAsInt(key string, defaultValue int) int {
	if value := os.Getenv(key); value != "" {
		var intValue int
		if _, err := fmt.Sscanf(value, "%d", &intValue); err == nil {
			return intValue
		}
	}
	return defaultValue
}
