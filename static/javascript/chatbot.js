class ChatbotWidget {
    constructor() {        
        this.userId = document.querySelector('[data-user-id]')?.dataset.userId || 'unknown';
        this.conversationId = null;
        this.isEscalated = false;
        
        this.toggleBtn = document.getElementById('chatbotToggle');
        this.chatWindow = document.getElementById('chatbotWindow');
        this.closeBtn = document.getElementById('chatbotClose');
        this.messagesContainer = document.getElementById('chatbotMessages');
        this.input = document.getElementById('chatbotInput');
        this.sendBtn = document.getElementById('chatbotSendBtn');
        this.faqBtns = document.querySelectorAll('.faq-btn');
        this.escalateBtn = document.getElementById('escalateBtn');
        this.unsubscribe = null;
        
        if (!this.toggleBtn) console.error('❌ Missing: chatbotToggle');
        if (!this.chatWindow) console.error('❌ Missing: chatbotWindow');
        if (!this.messagesContainer) console.error('❌ Missing: chatbotMessages');
        
        if (this.userId === 'unknown') {
            console.error('❌ User ID not found!');
            return;
        }
        this.init();
    }
    
    async init() {
        await this.getOrCreateConversationId();
        await this.checkEscalationStatus();
        this.setupEventListeners();
        if (this.isEscalated) {
            this.listenMessages();      // only listen if already escalated
            this.listenAdminTyping();
        }
        this.updateUIForEscalation();
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
            this.conversationId = convId;
            return;
        }

        // 2. Nothing in Firestore → create new
        const convId = 'conv_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
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
            }
        } catch (error) {
            console.error('Error checking escalation status:', error);
        }
    }
    
    setupEventListeners() {
        if (this.toggleBtn) {
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
        this.setupDraggable();
        this.setupFaqToggle();
    }

    setupDraggable() {
        if (window.innerWidth > 768) return;

        interact(this.toggleBtn).draggable({
            inertia: true,
            listeners: {
                move(event) {
                    const target = event.target;
                    const x = (parseFloat(target.getAttribute('data-x')) || 0) + event.dx;
                    const y = (parseFloat(target.getAttribute('data-y')) || 0) + event.dy;

                    // Get button size
                    const btnRect = target.getBoundingClientRect();
                    const btnW = btnRect.width;
                    const btnH = btnRect.height;

                    // Current position in viewport (before transform)
                    const currentLeft = btnRect.left - (parseFloat(target.getAttribute('data-x')) || 0);
                    const currentTop = btnRect.top - (parseFloat(target.getAttribute('data-y')) || 0);

                    // Clamp so it never goes outside viewport
                    const minX = -currentLeft + 10;
                    const minY = -currentTop + 10;
                    const maxX = window.innerWidth - currentLeft - btnW - 10;
                    const maxY = window.innerHeight - currentTop - btnH - 10;

                    const clampedX = Math.min(Math.max(x, minX), maxX);
                    const clampedY = Math.min(Math.max(y, minY), maxY);

                    target.style.transform = `translate(${clampedX}px, ${clampedY}px)`;
                    target.setAttribute('data-x', clampedX);
                    target.setAttribute('data-y', clampedY);
                },

                end(event) {
                    const target = event.target;
                    const btnRect = target.getBoundingClientRect();
                    const btnW = btnRect.width;
                    const btnH = btnRect.height;

                    const currentLeft = btnRect.left - (parseFloat(target.getAttribute('data-x')) || 0);
                    const currentTop  = btnRect.top  - (parseFloat(target.getAttribute('data-y')) || 0);

                    // Current center of button in viewport
                    const btnCenterX = btnRect.left + btnW / 2;

                    // Snap to left or right edge
                    const snapToRight = btnCenterX > window.innerWidth / 2;

                    const targetX = snapToRight
                        ? window.innerWidth - currentLeft - btnW - 10   // right edge
                        : -currentLeft + 10;                             // left edge

                    const currentY = parseFloat(target.getAttribute('data-y')) || 0;

                    // Clamp Y so it doesn't go off screen
                    const minY = -currentTop + 10;
                    const maxY = window.innerHeight - currentTop - btnH - 10;
                    const clampedY = Math.min(Math.max(currentY, minY), maxY);

                    // Smooth transition to edge
                    target.style.transition = 'transform 0.3s cubic-bezier(0.25, 0.46, 0.45, 0.94)';
                    target.style.transform = `translate(${targetX}px, ${clampedY}px)`;
                    target.setAttribute('data-x', targetX);
                    target.setAttribute('data-y', clampedY);

                    // Remove transition after snap so drag feels instant again
                    setTimeout(() => {
                        target.style.transition = '';
                    }, 300);
                }
            }
        });
    }
    
    toggleWindow() {
        if (!this.chatWindow) return;
        if (this.chatWindow.style.display === 'none' || !this.chatWindow.style.display) {
            this.chatWindow.style.display = 'flex';
            this.toggleBtn.style.display = 'none'; // hide toggle when open
            this.hideBadge();
            setTimeout(() => {
                this.messagesContainer.scrollTop = this.messagesContainer.scrollHeight;
            }, 50)
            if (this.input) this.input.focus();
        } else {
            this.chatWindow.style.display = 'none';
            this.toggleBtn.style.display = 'flex'; // show toggle when closed
        }
    }
    
    handleFaqClick(btn) {
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
                this.messagesContainer.innerHTML = ''; // clear local messages
                this.listenMessages();                 // ← start Firestore listener now
                this.listenAdminTyping();              // ← start admin typing listener
                this.updateUIForEscalation();
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
    setupFaqToggle() {
        const toggleBtn = document.getElementById('faqToggleBtn');
        const faqContainer = document.getElementById('faqButtons');
        const icon = document.getElementById('faqToggleIcon');

        if (!toggleBtn || !faqContainer) return;

        toggleBtn.addEventListener('click', () => {
            const isOpen = faqContainer.style.display === 'grid';
            faqContainer.style.display = isOpen ? 'none' : 'grid';
            icon.textContent = isOpen ? '▼' : '▲';
        });
    }
    
    addDateSeparator(date) {
        const today = new Date();
        const yesterday = new Date();
        yesterday.setDate(yesterday.getDate() - 1);

        let label = '';
        if (date.toDateString() === today.toDateString()) {
            label = 'Today';
        } else if (date.toDateString() === yesterday.toDateString()) {
            label = 'Yesterday';
        } else {
            label = date.toLocaleDateString([], { month: 'short', day: 'numeric' });
        }

        const separator = document.createElement('div');
        separator.className = 'date-separator';
        separator.innerHTML = `<span>${label}</span>`;
        this.messagesContainer.appendChild(separator);
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
            if (!document.getElementById('resetConversationBtn')) {
                const resetBtn = document.createElement('button');
                resetBtn.id = 'resetConversationBtn';
                resetBtn.className = 'btn-reset-conversation';
                resetBtn.textContent = '🔄 Start New Chat';
                resetBtn.addEventListener('click', () => this.resetConversation());
                this.escalateBtn.parentElement.appendChild(resetBtn);
            }
        }
    }
    async resetConversation() {
    try {
        const response = await fetch('/reset-conversation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                conversation_id: this.conversationId
            })
        });

        const data = await response.json();

        if (data.success) {
            if (this.unsubscribe) {
                this.unsubscribe();
                this.unsubscribe = null;
            }

            this.conversationId = data.new_conversation_id;
            this.isEscalated = false;

            this.messagesContainer.innerHTML = '';
            this.lastDateLabel = null;

            this.showFaqButtons();
            if (this.escalateBtn) {
                this.escalateBtn.textContent = '👤 Chat with Owner';
                this.escalateBtn.disabled = false;
                this.escalateBtn.style.opacity = '1';
                this.escalateBtn.style.cursor = 'pointer';
                this.escalateBtn.onclick = () => this.escalateToOwner();
            }

            const resetBtn = document.getElementById('resetConversationBtn');
            if (resetBtn) resetBtn.remove();

            this.addMessage('👋 Starting a new conversation! How can we help you?', 'bot');
        }
    } catch (error) {
        console.error('Error resetting conversation:', error);
        this.addMessage('Sorry, could not reset. Please try again.', 'bot');
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
                this.hideTyping();
                snapshot.docChanges().forEach((change) => {
                    if (change.type === 'added') {
                        const msg = change.doc.data();
                        const isWindowClosed = this.chatWindow.style.display === 'none';
                        
                        if (msg.sender === 'admin' && isWindowClosed) {
                            this.showBadge();
                        }
                    }
                });
                // Clear container and reload all messages
                this.messagesContainer.innerHTML = '';
                this.lastDateLabel = null;
                snapshot.forEach((doc) => {
                    const msg = doc.data();
                    if (msg.text && msg.sender) {
                        this.addMessage(msg.text, msg.sender, msg.timestamp, msg.order_context || null);
                    }
                });
                
                setTimeout(() => {
                    this.messagesContainer.scrollTop = this.messagesContainer.scrollHeight;
                }, 50);
            });
        } catch (error) {
            console.error('Error setting up listener:', error);
        }
    }
    
    async sendMessage() {
        const message = this.input?.value.trim();
        if (!message) return;
        if (this.input) this.input.value = '';
        if (this.input) this.input.placeholder = 'Ask a question...'; // reset placeholder

        // ← grab pending order context if any
        const orderContext = this.pendingOrderContext || null;
        this.pendingOrderContext = null;
        document.getElementById('pendingOrderContextCard')?.remove();

        this.addMessage(message, 'customer', new Date());
        if (!this.isEscalated) this.showTyping();
        try {
            const response = await fetch('/send-message', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message: message,
                    user_id: this.userId,
                    conversation_id: this.conversationId,
                    is_escalation: false,
                    order_context: orderContext  // ← attach it
                })
            });
            
            const data = await response.json();
            this.hideTyping();
            if (!data.success) {
                console.error('Failed to send:', data.error);
                this.addMessage('Sorry, there was an error. Please try again.', 'bot');
            } else if (data.escalated && !this.isEscalated) {
                // ← auto-escalated via order context — switch to owner chat mode
                this.isEscalated = true;
                this.messagesContainer.innerHTML = '';
                this.lastDateLabel = null;
                this.listenMessages();
                this.listenAdminTyping();
                this.updateUIForEscalation();
            } else if (!this.isEscalated && data.reply) {
                this.addMessage(data.reply, 'bot', new Date());
            }
        } catch (error) {
            this.hideTyping();
            console.error('Error sending message:', error);
            this.addMessage('Connection error. Please check your internet connection.', 'bot');
        }
    }
    showTyping() {
        const typing = document.createElement('div');
        typing.id = 'typingIndicator';
        typing.className = 'message bot-message';
        typing.innerHTML = `<p class="typing-bubble"><span></span><span></span><span></span></p>`;
        this.messagesContainer.appendChild(typing);
        this.messagesContainer.scrollTop = this.messagesContainer.scrollHeight;
    }

    hideTyping() {
        const typing = document.getElementById('typingIndicator');
        if (typing) typing.remove();
    }
    listenAdminTyping() {
        if (!this.userId || !this.conversationId) return;
        
        const db = firebase.firestore();
        db.collection('users').doc(this.userId)
            .collection('conversations').doc(this.conversationId)
            .onSnapshot((doc) => {
                if (!doc.exists) return;
                const data = doc.data();
                
                if (data.is_typing && this.isEscalated) {
                    this.showAdminTyping();
                } else {
                    this.hideAdminTyping();
                }
            });
    }

    showAdminTyping() {
        if (document.getElementById('adminTypingIndicator')) return;
        const typing = document.createElement('div');
        typing.id = 'adminTypingIndicator';
        typing.className = 'message bot-message';
        typing.innerHTML = `<p class="typing-bubble">👤 <span></span><span></span><span></span></p>`;
        this.messagesContainer.appendChild(typing);
        this.messagesContainer.scrollTop = this.messagesContainer.scrollHeight;
    }

    hideAdminTyping() {
        const typing = document.getElementById('adminTypingIndicator');
        if (typing) typing.remove();
    }
showBadge() {
    let badge = document.getElementById('chatbotBadge');
    if (!badge) {
        badge = document.createElement('span');
        badge.id = 'chatbotBadge';
        badge.style.cssText = `
            position: absolute;
            top: 0;
            right: 0;
            background: red;
            border-radius: 50%;
            width: 14px;
            height: 14px;
            border: 2px solid white;
            pointer-events: none;
        `;
        this.toggleBtn.appendChild(badge);
    }
    badge.style.display = 'block';
}

    hideBadge() {
        const badge = document.getElementById('chatbotBadge');
        if (badge) badge.remove();
        this.unreadCount = 0;
    }


    addMessage(text, sender, timestamp, orderContext) {
        if (!this.messagesContainer) return;
        if (timestamp) {
            const date = timestamp.toDate ? timestamp.toDate() : new Date(timestamp);
            const dateStr = date.toDateString();
            if (dateStr !== this.lastDateLabel) {
                this.lastDateLabel = dateStr;
                this.addDateSeparator(date);
            }
        }
        const msgDiv = document.createElement('div');

        let timeStr = '';
        if (timestamp) {
            const date = timestamp.toDate ? timestamp.toDate() : new Date(timestamp);
            timeStr = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }
        const timeHtml = timeStr ? `<span class="msg-timestamp-inline">${timeStr}</span>` : '';

        // Build order context card if present
        let orderCardHtml = '';
        if (orderContext) {
            const itemDetails = orderContext.item_details
                ? `<div style="font-size:11px;color:#888;margin-bottom:3px;">${this.escapeHtml(orderContext.item_details)}</div>`
                : '';
            const typeLabel = orderContext.order_type === 'custom' ? 'Custom' : 'Premade';
            const amount = parseFloat(orderContext.amount || 0).toFixed(2);
            orderCardHtml = `
            
                <div style="background:#fff0f6;border:1.5px solid #f5c2dc;border-radius:8px;
                            padding:9px 12px;margin-bottom:6px;display:flex;gap:10px;align-items:center;">
                    ${orderContext.item_image
                        ? `<img src="${orderContext.item_image}" style="width:48px;height:48px;border-radius:6px;object-fit:cover;flex-shrink:0;">`
                        : `<div style="width:48px;height:48px;border-radius:6px;background:#f5c2dc;display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0;">🎂</div>`}
                    <div>
                        <div style="font-size:12px;font-weight:700;color:#d63384;margin-bottom:3px;">
                            ${orderContext.item_label || 'Order'}
                        </div>
                        ${itemDetails ? `<div style="font-size:11px;color:#aaa;margin-bottom:3px;">${orderContext.item_details}</div>` : ''}
                        <div style="font-size:11px;color:#aaa;">
                            ${typeLabel} · ₱${amount} · ${orderContext.status || ''}
                        </div>
                        <div style="font-size:10px;color:#bbb;margin-top:3px;font-family:monospace;">
                            Order ID #${orderContext.order_id}
                        </div>
                    </div>
                </div>`;
        }

        if (sender === 'customer') {
            msgDiv.className = 'message customer-message';
            msgDiv.innerHTML = `${orderCardHtml}<p>${this.escapeHtml(text)} ${timeHtml}</p>`;
        } else if (sender === 'admin') {
            msgDiv.className = 'message bot-message';
            msgDiv.innerHTML = `
                ${orderCardHtml}
                <p>👤 <strong>Owner:</strong> ${this.escapeHtml(text)} ${timeHtml}</p>`;
        } else if (sender === 'bot') {
            msgDiv.className = 'message bot-message';
            msgDiv.innerHTML = `<p>${this.escapeHtml(text)} ${timeHtml}</p>`;
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
class GuestChatbotWidget {
    constructor() {
        this.storageKey = 'guestChatHistory';
        this.toggleBtn = document.getElementById('chatbotToggle');
        this.chatWindow = document.getElementById('chatbotWindow');
        this.closeBtn = document.getElementById('chatbotClose');
        this.messagesContainer = document.getElementById('chatbotMessages');
        this.input = document.getElementById('chatbotInput');
        this.sendBtn = document.getElementById('chatbotSendBtn');
        this.faqBtns = document.querySelectorAll('.faq-btn');
        this.setupEventListeners();
        this.setupFaqToggle();
        this.loadHistory();
    }

    setupEventListeners() {
        this._toggleHandler = () => this.toggleWindow();
        this.toggleBtn?.addEventListener('click', this._toggleHandler);
        this.closeBtn?.addEventListener('click', () => this.toggleWindow());
        this.sendBtn?.addEventListener('click', () => this.sendMessage());
        this.input?.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') this.sendMessage();
        });
        this.faqBtns.forEach(btn => {
            btn.addEventListener('click', () => {
                if (this.input) {
                    this.input.value = btn.dataset.question;
                    this.sendMessage();
                }
            });
        });
        this.setupDraggable();
    }
    setupDraggable() {
        if (window.innerWidth > 768) return;

        interact(this.toggleBtn).draggable({
            inertia: true,
            listeners: {
                move(event) {
                    const target = event.target;
                    const x = (parseFloat(target.getAttribute('data-x')) || 0) + event.dx;
                    const y = (parseFloat(target.getAttribute('data-y')) || 0) + event.dy;

                    // Get button size
                    const btnRect = target.getBoundingClientRect();
                    const btnW = btnRect.width;
                    const btnH = btnRect.height;

                    // Current position in viewport (before transform)
                    const currentLeft = btnRect.left - (parseFloat(target.getAttribute('data-x')) || 0);
                    const currentTop = btnRect.top - (parseFloat(target.getAttribute('data-y')) || 0);

                    // Clamp so it never goes outside viewport
                    const minX = -currentLeft + 10;
                    const minY = -currentTop + 10;
                    const maxX = window.innerWidth - currentLeft - btnW - 10;
                    const maxY = window.innerHeight - currentTop - btnH - 10;

                    const clampedX = Math.min(Math.max(x, minX), maxX);
                    const clampedY = Math.min(Math.max(y, minY), maxY);

                    target.style.transform = `translate(${clampedX}px, ${clampedY}px)`;
                    target.setAttribute('data-x', clampedX);
                    target.setAttribute('data-y', clampedY);
                },

                end(event) {
                    const target = event.target;
                    const btnRect = target.getBoundingClientRect();
                    const btnW = btnRect.width;
                    const btnH = btnRect.height;

                    const currentLeft = btnRect.left - (parseFloat(target.getAttribute('data-x')) || 0);
                    const currentTop  = btnRect.top  - (parseFloat(target.getAttribute('data-y')) || 0);

                    // Current center of button in viewport
                    const btnCenterX = btnRect.left + btnW / 2;

                    // Snap to left or right edge
                    const snapToRight = btnCenterX > window.innerWidth / 2;

                    const targetX = snapToRight
                        ? window.innerWidth - currentLeft - btnW - 10   // right edge
                        : -currentLeft + 10;                             // left edge

                    const currentY = parseFloat(target.getAttribute('data-y')) || 0;

                    // Clamp Y so it doesn't go off screen
                    const minY = -currentTop + 10;
                    const maxY = window.innerHeight - currentTop - btnH - 10;
                    const clampedY = Math.min(Math.max(currentY, minY), maxY);

                    // Smooth transition to edge
                    target.style.transition = 'transform 0.3s cubic-bezier(0.25, 0.46, 0.45, 0.94)';
                    target.style.transform = `translate(${targetX}px, ${clampedY}px)`;
                    target.setAttribute('data-x', targetX);
                    target.setAttribute('data-y', clampedY);

                    // Remove transition after snap so drag feels instant again
                    setTimeout(() => {
                        target.style.transition = '';
                    }, 300);
                }
            }
        });
    }
    setupFaqToggle() {
        const toggleBtn = document.getElementById('faqToggleBtn');
        const faqContainer = document.getElementById('faqButtons');
        const icon = document.getElementById('faqToggleIcon');
        if (!toggleBtn || !faqContainer) return;
        toggleBtn.addEventListener('click', () => {
            const isOpen = faqContainer.style.display === 'grid';
            faqContainer.style.display = isOpen ? 'none' : 'grid';
            if (icon) icon.textContent = isOpen ? '▼' : '▲';
        });
    }

    toggleWindow() {
        if (!this.chatWindow) return;
        const isHidden = this.chatWindow.style.display === 'none' || !this.chatWindow.style.display;
        this.chatWindow.style.display = isHidden ? 'flex' : 'none';
        this.toggleBtn.style.display = isHidden ? 'none' : 'flex'; // toggle visibility
        if (isHidden && this.input) this.input.focus();
        if (isHidden) {
            setTimeout(() => {
                this.messagesContainer.scrollTop = this.messagesContainer.scrollHeight;
            }, 50);
        }
    }

    loadHistory() {
        const history = JSON.parse(localStorage.getItem(this.storageKey) || '[]');
        if (history.length === 0) return;
        this.messagesContainer.innerHTML = '';
        history.forEach(msg => this.addMessage(msg.text, msg.sender, msg.time ? new Date(msg.time) : null));
    }

    saveMessage(sender, text) {
        const history = JSON.parse(localStorage.getItem(this.storageKey) || '[]');
        history.push({ sender, text, time: Date.now() });
        localStorage.setItem(this.storageKey, JSON.stringify(history));
    }

    async sendMessage() {
        const message = this.input?.value.trim();
        if (!message) return;
        this.input.value = '';
        this.addMessage(message, 'customer', new Date());
        this.saveMessage('customer', message);
        this.showTyping();
        try {
            const response = await fetch('/send-message', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message: message,
                    user_id: 'guest',
                    conversation_id: 'guest_session',
                    is_escalation: false
                })
            });
            const data = await response.json();
            this.hideTyping();
            if (data.reply) {
                this.addMessage(data.reply, 'bot', new Date());
                this.saveMessage('bot', data.reply);
            }
        } catch (error) {
            this.hideTyping();
            this.addMessage('Connection error. Please try again.', 'bot');
        }
    }

    showTyping() {
        const typing = document.createElement('div');
        typing.id = 'typingIndicator';
        typing.className = 'message bot-message';
        typing.innerHTML = `<p class="typing-bubble"><span></span><span></span><span></span></p>`;
        this.messagesContainer.appendChild(typing);
        this.messagesContainer.scrollTop = this.messagesContainer.scrollHeight;
    }

    hideTyping() {
        document.getElementById('typingIndicator')?.remove();
    }

    addMessage(text, sender, timestamp) {
        if (!this.messagesContainer) return;
        const msgDiv = document.createElement('div');
        
        const timeStr = timestamp
            ? new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
            : '';
        const timeHtml = timeStr ? `<span class="msg-timestamp-inline">${timeStr}</span>` : '';

        if (sender === 'customer') {
            msgDiv.className = 'message customer-message';
            msgDiv.innerHTML = `<p>${this.escapeHtml(text)} ${timeHtml}</p>`;
        } else {
            msgDiv.className = 'message bot-message';
            msgDiv.innerHTML = `<p>${this.escapeHtml(text)} ${timeHtml}</p>`;
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
    const userContainer = document.querySelector('[data-user-id]');
    const userId = userContainer?.dataset.userId;
    if (userId && userId !== 'None' && userId !== '') {
        firebase.auth().onAuthStateChanged((user) => {
            if (user) {
                window.chatbot = new ChatbotWidget();
            }
        });
    } else {
        // Guest — no login required
        window.chatbot = new GuestChatbotWidget();
    }
});