package sse

import (
	"log"
	"sync"
)

type Broker struct {
	clients sync.Map // map[string]chan string
}

func NewBroker() *Broker {
	return &Broker{}
}

func (b *Broker) Subscribe(clientID string) chan string {
	ch := make(chan string, 100)
	b.clients.Store(clientID, ch)
	log.Printf("Client %s subscribed to SSE", clientID)
	return ch
}

func (b *Broker) Unsubscribe(clientID string) {
	if ch, ok := b.clients.LoadAndDelete(clientID); ok {
		close(ch.(chan string))
		log.Printf("Client %s unsubscribed from SSE", clientID)
	}
}

func (b *Broker) SendEvent(clientID string, event string) {
	if ch, ok := b.clients.Load(clientID); ok {
		select {
		case ch.(chan string) <- event:
		default:
			log.Printf("Channel full for client %s, dropping event", clientID)
		}
	}
}

func (b *Broker) SendToAll(event string) {
	b.clients.Range(func(key, value interface{}) bool {
		select {
		case value.(chan string) <- event:
		default:
			log.Printf("Channel full for client %s, dropping event", key)
		}
		return true
	})
}

func (b *Broker) Close() {
	b.clients.Range(func(key, value interface{}) bool {
		close(value.(chan string))
		b.clients.Delete(key)
		return true
	})
}
