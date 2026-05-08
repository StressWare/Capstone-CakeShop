class ChatbotWidget {
    constructor() {
        console.log('🏗️ Constructing ChatbotWidget...');
        
        this.userId = document.querySelector('[data-user-id]')?.dataset.userId || 'unknown';
        this.conversationId = null;
        this.isEscalated = false;
        
        this.toggleBtn = document.getElementById('chatbotToggle');
        this.window = document.getElementById('chatbotWindow');
        this.closeBtn = document.getElementById('chatbotClose');
        this.messagesContainer = document.getElementById('chatbotMessages');
        this.input = document.getElementById('chatbotInput');
        this.sendBtn = document.getElementById('chatbotSendBtn');
        this.faqBtns = document.querySelectorAll('.faq-btn');
        this.escalateBtn = document.getElementById('escalateBtn');
        this.unsubscribe = null;
        
        if (!this.toggleBtn) console.error('❌ Missing: chatbotToggle');
        if (!this.window) console.error('❌ Missing: chatbotWindow');
        if (!this.messagesContainer) console.error('❌ Missing: chatbotMessages');
        
        if (this.userId === 'unknown') {
            console.error('❌ User ID not found!');
            return;
        }
        
        console.log('✅ User ID:', this.userId);
        this.init();
    }
    
    async init() {
        console.log('🚀 Initializing chatbot...');
        await this.getOrCreateConversationId();
        await this.checkEscalationStatus();
        this.setupEventListeners();
        this.listenMessages();
        this.updateUIForEscalation();
        console.log('✅ Chatbot initialized successfully');
    }
    
    async getOrCreateConversationId() {
        const db = firebase.firestore();

        // 1. Check Firestore first — works on any device
        const snapshot = await db.collection('users')
            .doc(this.userId)
            .collection('conversations')
            .orderBy('created_at', 'desc')
            .limit(1)
            .get();

        if (!snapshot.empty) {
            const convId = snapshot.docs[0].id;
            localStorage.setItem(`chatbot_conversation_${this.userId}`, convId); // keep local in sync
            console.log('✅ Found existing conversation:', convId);
            this.conversationId = convId;
            return;
        }

        // 2. Nothing in Firestore → create new
        const convId = 'conv_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
        console.log('Created new conversation ID:', convId);

        try {
            await db.collection('users')
                .doc(this.userId)
                .collection('conversations')
                .doc(convId)
                .set({
                    created_at:   firebase.firestore.FieldValue.serverTimestamp(),
                    last_updated: firebase.firestore.FieldValue.serverTimestamp(),
                    escalated:    false
                });

            localStorage.setItem(`chatbot_conversation_${this.userId}`, convId);
            console.log('✅ New conversation saved to Firestore');
        } catch (error) {
            console.error('❌ Error creating conversation:', error);
        }

        this.conversationId = convId;
    }
    
    async checkEscalationStatus() {
        try {
            const response = await fetch(`/conversation-status/${this.userId}/${this.conversationId}`);
            const data = await response.json();
            
            if (data.success) {
                this.isEscalated = data.escalated;
                console.log('Escalation status:', this.isEscalated);
            }
        } catch (error) {
            console.error('Error checking escalation status:', error);
        }
    }
    
    setupEventListeners() {
        console.log('Setting up event listeners...');
        
        if (this.toggleBtn) {
            console.log('✅ Toggle button found, adding click listener');
            this.toggleBtn.addEventListener('click', () => this.toggleWindow());
        }
        
        if (this.closeBtn) {
            this.closeBtn.addEventListener('click', () => this.toggleWindow());
        }
        
        if (this.sendBtn) {
            this.sendBtn.addEventListener('click', () => this.sendMessage());
        }
        
        if (this.input) {
            this.input.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') this.sendMessage();
            });
        }
        
        this.faqBtns.forEach(btn => {
            btn.addEventListener('click', () => this.handleFaqClick(btn));
        });
        
        if (this.escalateBtn && !this.isEscalated) {
            this.escalateBtn.addEventListener('click', () => this.escalateToOwner());
        }
    }
    
    toggleWindow() {
        console.log('🪟 Toggle window called, current display:', this.window?.style.display);
        
        if (!this.window) return;
        
        if (this.window.style.display === 'none' || !this.window.style.display) {
            this.window.style.display = 'flex';
            console.log('✅ Window opened');
            if (this.input) this.input.focus();
        } else {
            this.window.style.display = 'none';
            console.log('✅ Window closed');
        }
    }
    
    handleFaqClick(btn) {
        console.log('❓ FAQ clicked:', btn.dataset.question);
        
        if (this.isEscalated) {
            this.addMessage("You're now chatting with the owner. Please wait for their response.", 'bot');
            return;
        }
        
        if (this.input) {
            this.input.value = btn.dataset.question;
            this.sendMessage();
        }
    }
    
    async escalateToOwner() {
        console.log('👤 Escalate button clicked');
        
        if (this.isEscalated) {
            this.addMessage("You're already connected with the owner.", 'bot');
            return;
        }
        
        this.hideFaqButtons();
        
        if (this.escalateBtn) {
            this.escalateBtn.disabled = true;
            this.escalateBtn.textContent = '⏳ Connecting...';
        }
        
        try {
            const response = await fetch('/send-message', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message: "Customer requested to speak with owner",
                    user_id: this.userId,
                    conversation_id: this.conversationId,
                    is_escalation: true
                })
            });
            
            const data = await response.json();
            
            if (data.success) {
                this.isEscalated = true;
                this.updateUIForEscalation();
                console.log('✅ Escalated successfully');
            }
        } catch (error) {
            console.error('Error escalating:', error);
            this.addMessage("Sorry, we're having trouble connecting you.", 'bot');
            this.showFaqButtons();
            
            if (this.escalateBtn) {
                this.escalateBtn.disabled = false;
                this.escalateBtn.textContent = '👤 Chat with Owner';
            }
        }
    }
    
    hideFaqButtons() {
        this.faqBtns.forEach(btn => {
            btn.style.display = 'none';
        });
        
        const faqContainer = document.querySelector('.faq-buttons');
        if (faqContainer) {
            faqContainer.style.display = 'none';
        }
    }
    
    showFaqButtons() {
        this.faqBtns.forEach(btn => {
            btn.style.display = '';
        });
        
        const faqContainer = document.querySelector('.faq-buttons');
        if (faqContainer) {
            faqContainer.style.display = '';
        }
    }
    
    updateUIForEscalation() {
        if (this.isEscalated) {
            if (this.escalateBtn) {
                this.escalateBtn.textContent = '👤 Chatting with Owner';
                this.escalateBtn.disabled = true;
                this.escalateBtn.style.opacity = '0.6';
                this.escalateBtn.style.cursor = 'not-allowed';
            }
            
            this.hideFaqButtons();
            
            const hasSystemMsg = Array.from(this.messagesContainer.children).some(
                child => child.textContent && child.textContent.includes('connected with the shop owner')
            );
            
            if (!hasSystemMsg) {
                this.addMessage('You are now connected with the shop owner. The bot will no longer respond automatically.', 'bot');
            }
        }
    }
    
    listenMessages() {
        if (!this.userId || !this.conversationId || typeof firebase === 'undefined') return;
        
        try {
            const db = firebase.firestore();
            const messagesRef = db
                .collection('users').doc(this.userId)
                .collection('conversations').doc(this.conversationId)
                .collection('messages')
                .orderBy('timestamp', 'asc');
            
            if (this.unsubscribe) this.unsubscribe();
            
            this.unsubscribe = messagesRef.onSnapshot((snapshot) => {
                // Clear container and reload all messages
                this.messagesContainer.innerHTML = '';
                
                snapshot.forEach((doc) => {
                    const msg = doc.data();
                    if (msg.text && msg.sender) {
                        this.addMessage(msg.text, msg.sender);
                    }
                });
                
                this.messagesContainer.scrollTop = this.messagesContainer.scrollHeight;
            });
        } catch (error) {
            console.error('Error setting up listener:', error);
        }
    }
    
    async sendMessage() {
        const message = this.input?.value.trim();
        if (!message) return;
        
        
        if (this.input) this.input.value = '';
        
        try {
            const response = await fetch('/send-message', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message: message,
                    user_id: this.userId,
                    conversation_id: this.conversationId,
                    is_escalation: false
                })
            });
            
            const data = await response.json();
            if (!data.success) {
                console.error('Failed to send:', data.error);
                this.addMessage('Sorry, there was an error. Please try again.', 'bot');
            }
        } catch (error) {
            console.error('Error sending message:', error);
            this.addMessage('Connection error. Please check your internet connection.', 'bot');
        }
    }
    
    addMessage(text, sender) {
        if (!this.messagesContainer) return;
        
        const msgDiv = document.createElement('div');
        
        if (sender === 'customer') {
            msgDiv.className = 'message customer-message';
            msgDiv.innerHTML = `<p>${this.escapeHtml(text)}</p>`;
        } else if (sender === 'admin') {
            msgDiv.className = 'message bot-message';
            msgDiv.innerHTML = `<p>👤 <strong>Owner:</strong> ${this.escapeHtml(text)}</p>`;
        } else if (sender === 'bot') {
            msgDiv.className = 'message bot-message';
            msgDiv.innerHTML = `<p>${this.escapeHtml(text)}</p>`;
        } else {
            return;
        }
        
        this.messagesContainer.appendChild(msgDiv);
        this.messagesContainer.scrollTop = this.messagesContainer.scrollHeight;
    }
    
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Initialize
document.addEventListener('DOMContentLoaded', function() {
    console.log('📄 DOM loaded');
    
    const userContainer = document.querySelector('[data-user-id]');
    console.log('User container:', userContainer);
    console.log('User ID:', userContainer?.dataset.userId);
    console.log('Chatbot toggle button:', document.getElementById('chatbotToggle'));
    console.log('Chatbot window:', document.getElementById('chatbotWindow'));
    
    if (userContainer) {
        console.log('🚀 Creating ChatbotWidget instance...');
        window.chatbot = new ChatbotWidget();
    } else {
        console.error('❌ No user ID found - chatbot will not work!');
    }
});