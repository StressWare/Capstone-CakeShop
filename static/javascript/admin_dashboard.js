
// ============================================================================
// CONFIRMATION DIALOG
// ============================================================================
function confirmStatus(currentStatus) {
  return confirm(
    "Current status: " + currentStatus +
    "\n\nAre you sure you want to update this order?"
  );
}

// ============================================================================
// UNIVERSAL PAGINATION BUTTON GENERATOR
// ============================================================================
function updatePaginationButtons(containerId, currentPage, totalPages, onPageClick) {
  const container = document.getElementById(containerId);
  if (!container) return;
  
  let html = '';
  
  // Previous button
  html += `<li class="page-item ${currentPage === 1 ? 'disabled' : ''}">
    <a class="page-link" href="#" data-page="${currentPage - 1}">Previous</a>
  </li>`;
  
  // Page numbers
  for (let i = 1; i <= totalPages; i++) {
    if (totalPages <= 5 || i === 1 || i === totalPages || (i >= currentPage - 1 && i <= currentPage + 1)) {
      html += `<li class="page-item ${i === currentPage ? 'active' : ''}">
        <a class="page-link" href="#" data-page="${i}">${i}</a>
      </li>`;
    } else if (i === currentPage - 2 || i === currentPage + 2) {
      html += `<li class="page-item disabled"><a class="page-link" href="#">...</a></li>`;
    }
  }
  
  // Next button
  html += `<li class="page-item ${currentPage === totalPages || totalPages === 0 ? 'disabled' : ''}">
    <a class="page-link" href="#" data-page="${currentPage + 1}">Next</a>
  </li>`;
  
  container.innerHTML = html;
  
  // Attach click events
  container.querySelectorAll('a.page-link').forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      const page = parseInt(link.getAttribute('data-page'));
      if (page && page !== currentPage && page >= 1 && page <= totalPages) {
        onPageClick(page);
      }
    });
  });
}

// ============================================================================
// SECTION 1: SALES - SEARCH + FILTER + PAGINATION (ALL IN ONE)
// ============================================================================
let currentSalesPage = 1;
const salesPerPage = 10;

function updateSalesDisplay() {
  const searchValue = document.getElementById('searchSales').value.toLowerCase();
  const filterValue = document.getElementById('orderFilterSales').value;
  
  const allRows = document.querySelectorAll('#salesTable tbody tr');
  let visibleRows = [];
  
  // Apply BOTH search AND filter together
  allRows.forEach(row => {
    const text = row.textContent.toLowerCase();
    
    const matchSearch = text.includes(searchValue);
    const matchFilter = !filterValue || text.includes(filterValue);
    
    // BOTH must be true
    if (matchSearch && matchFilter) {
      visibleRows.push(row);
    }
  });
  
  // Pagination
  const totalPages = Math.ceil(visibleRows.length / salesPerPage);
  const start = (currentSalesPage - 1) * salesPerPage;
  const end = start + salesPerPage;
  
  // Hide all first
  allRows.forEach(row => row.style.display = 'none');
  
  // Show only current page
  visibleRows.forEach((row, index) => {
    if (index >= start && index < end) {
      row.style.display = '';
    }
  });
  
  updatePaginationButtons('salesPagination', currentSalesPage, totalPages, (page) => {
    currentSalesPage = page;
    updateSalesDisplay();
  });
}

// Event listeners for Sales
document.addEventListener('DOMContentLoaded', function() {
  document.getElementById('searchSales').addEventListener('keyup', () => {
    currentSalesPage = 1;
    updateSalesDisplay();
  });
  
  document.getElementById('orderFilterSales').addEventListener('change', () => {
    currentSalesPage = 1;
    updateSalesDisplay();
  });
  
  updateSalesDisplay(); // Initial load
});

let currentExpensesPage = 1;
const expensesPerPage = 10;

function updateExpensesDisplay() {
  const searchValue = document.getElementById('searchExpenses').value.toLowerCase();
  
  const allRows = document.querySelectorAll('#expensesTable tbody tr');
  let visibleRows = [];
  
  // Apply search
  allRows.forEach(row => {
    const text = row.textContent.toLowerCase();
    
    if (text.includes(searchValue)) {
      visibleRows.push(row);
    }
  });
  
  // Pagination
  const totalPages = Math.ceil(visibleRows.length / expensesPerPage);
  const start = (currentExpensesPage - 1) * expensesPerPage;
  const end = start + expensesPerPage;
  
  // Hide all first
  allRows.forEach(row => row.style.display = 'none');
  
  // Show only current page
  visibleRows.forEach((row, index) => {
    if (index >= start && index < end) {
      row.style.display = '';
    }
  });
  
  updatePaginationButtons('expensesPagination', currentExpensesPage, totalPages, (page) => {
    currentExpensesPage = page;
    updateExpensesDisplay();
  });
}

// Event listeners for Expenses
document.addEventListener('DOMContentLoaded', function() {
  document.getElementById('searchExpenses').addEventListener('keyup', () => {
    currentExpensesPage = 1;
    updateExpensesDisplay();
  });
  
  updateExpensesDisplay(); // Initial load
});

// ============================================================================
// SECTION 3: INVENTORY - SEARCH + FILTER + PAGINATION (ALL IN ONE)
// ============================================================================
let currentInventoryPage = 1;
const inventoryPerPage = 10;

function updateInventoryDisplay() {
  const searchValue = document.getElementById('searchInventory').value.toLowerCase();
  const statusValue = document.getElementById('statusFilterInventory').value;
  
  const allRows = document.querySelectorAll('#inventoryTable tbody tr');
  let visibleRows = [];
  
  // Apply BOTH search AND filter together
  allRows.forEach(row => {
    const text = row.textContent.toLowerCase();
    
    const matchSearch = text.includes(searchValue);
    
    // Status filter logic
    let matchStatus = true;
    if (statusValue === 'critical') {
      matchStatus = row.classList.contains('table-danger');
    } else if (statusValue === 'low') {
      matchStatus = row.classList.contains('table-warning');
    } else if (statusValue === 'good') {
      matchStatus = !row.classList.contains('table-danger') && !row.classList.contains('table-warning');
    }
    
    // BOTH must be true
    if (matchSearch && matchStatus) {
      visibleRows.push(row);
    }
  });
  
  // Pagination
  const totalPages = Math.ceil(visibleRows.length / inventoryPerPage);
  const start = (currentInventoryPage - 1) * inventoryPerPage;
  const end = start + inventoryPerPage;
  
  // Hide all first
  allRows.forEach(row => row.style.display = 'none');
  
  // Show only current page
  visibleRows.forEach((row, index) => {
    if (index >= start && index < end) {
      row.style.display = '';
    }
  });
  
  updatePaginationButtons('inventoryPagination', currentInventoryPage, totalPages, (page) => {
    currentInventoryPage = page;
    updateInventoryDisplay();
  });
}

// Event listeners for Inventory
document.addEventListener('DOMContentLoaded', function() {
  document.getElementById('searchInventory').addEventListener('keyup', () => {
    currentInventoryPage = 1;
    updateInventoryDisplay();
  });
  
  document.getElementById('statusFilterInventory').addEventListener('change', () => {
    currentInventoryPage = 1;
    updateInventoryDisplay();
  });
  
  updateInventoryDisplay(); // Initial load
});

// ============================================================================
// SECTION 4: ORDERS - SEARCH + FILTERS + PAGINATION (ALL IN ONE)
// ============================================================================
let currentOrdersPage = 1;
const ordersPerPage = 6;

function updateOrdersDisplay() {
  const searchValue = document.getElementById('searchOrders').value.toLowerCase();
  const statusValue = document.getElementById('statusFilter').value;
  const rushValue = document.getElementById('rushFilter').value;
  
  const allRows = document.querySelectorAll('#ordersTable tbody tr');
  let visibleRows = [];
  
  // Apply ALL filters together
  allRows.forEach(row => {
    const text = row.textContent.toLowerCase();
    
    // Search match
    const matchSearch = text.includes(searchValue);
    
    // Status match
    const statusBadge = row.querySelector('td:nth-child(5) .badge');
    const matchStatus = !statusValue || (statusBadge && statusBadge.textContent.trim() === statusValue);
    
    // Rush match
    const isRush = row.classList.contains('table-danger');
    const matchRush = !rushValue || (rushValue === 'yes' && isRush) || (rushValue === 'no' && !isRush);
    
    // ALL must be true
    if (matchSearch && matchStatus && matchRush) {
      visibleRows.push(row);
    }
  });
  
  // Pagination
  const totalPages = Math.ceil(visibleRows.length / ordersPerPage);
  const start = (currentOrdersPage - 1) * ordersPerPage;
  const end = start + ordersPerPage;
  
  // Hide all first
  allRows.forEach(row => row.style.display = 'none');
  
  // Show only current page
  visibleRows.forEach((row, index) => {
    if (index >= start && index < end) {
      row.style.display = '';
    }
  });
  
  updatePaginationButtons('ordersPagination', currentOrdersPage, totalPages, (page) => {
    currentOrdersPage = page;
    updateOrdersDisplay();
  });
}

// Event listeners for Orders
document.addEventListener('DOMContentLoaded', function() {
  document.getElementById('searchOrders').addEventListener('keyup', () => {
    currentOrdersPage = 1;
    updateOrdersDisplay();
  });
  
  document.getElementById('statusFilter').addEventListener('change', () => {
    currentOrdersPage = 1;
    updateOrdersDisplay();
  });
  
  document.getElementById('rushFilter').addEventListener('change', () => {
    currentOrdersPage = 1;
    updateOrdersDisplay();
  });
  
  updateOrdersDisplay(); // Initial load
});

// ============================================================================
// SECTION 5: CAKES - SEARCH + FILTERS + PAGINATION (ALL IN ONE)
// ============================================================================
let currentCakesPage = 1;
const cakesPerPage = 8;

function updateCakesDisplay() {
  const searchValue = document.getElementById('searchCakes').value.toLowerCase();
  const categoryValue = document.getElementById('categoryFilter').value;
  const statusValue = document.getElementById('cakeStatusFilter').value;
  
  const allItems = document.querySelectorAll('.cake-item');
  let visibleItems = [];
  
  // Apply ALL filters together
  allItems.forEach(item => {
    const name = item.getAttribute('data-name');
    const category = item.getAttribute('data-category');
    const status = item.getAttribute('data-status');
    
    const matchSearch = name.includes(searchValue);
    const matchCategory = !categoryValue || category === categoryValue;
    const matchStatus = !statusValue || status === statusValue;
    
    // ALL must be true
    if (matchSearch && matchCategory && matchStatus) {
      visibleItems.push(item);
    }
  });
  
  // Pagination
  const totalPages = Math.ceil(visibleItems.length / cakesPerPage);
  const start = (currentCakesPage - 1) * cakesPerPage;
  const end = start + cakesPerPage;
  
  // Hide all first
  allItems.forEach(item => item.style.display = 'none');
  
  // Show only current page
  visibleItems.forEach((item, index) => {
    if (index >= start && index < end) {
      item.style.display = '';
    }
  });
  
  updatePaginationButtons('cakesPagination', currentCakesPage, totalPages, (page) => {
    currentCakesPage = page;
    updateCakesDisplay();
  });
}

// Event listeners for Cakes
document.addEventListener('DOMContentLoaded', function() {
  document.getElementById('searchCakes').addEventListener('keyup', () => {
    currentCakesPage = 1;
    updateCakesDisplay();
  });
  
  document.getElementById('categoryFilter').addEventListener('change', () => {
    currentCakesPage = 1;
    updateCakesDisplay();
  });
  
  document.getElementById('cakeStatusFilter').addEventListener('change', () => {
    currentCakesPage = 1;
    updateCakesDisplay();
  });
  
  updateCakesDisplay(); // Initial load
});

// ============================================================================
// EDIT ORDER MODAL - Calculate total price
// ============================================================================
function calculateEditTotal(orderId) {
  const cakePrice = parseFloat(document.getElementById(`editCakeType${orderId}`).value) || 0;
  const designPrice = parseFloat(document.getElementById(`editDesign${orderId}`).value) || 0;
  const sizePrice = parseFloat(document.getElementById(`editSize${orderId}`).value) || 0;
  const layersPrice = parseFloat(document.getElementById(`editLayers${orderId}`).value) || 0;

  let toppingsPrice = 0;
  document.querySelectorAll(`.edit-topping[data-order-id="${orderId}"]:checked`).forEach(t => {
    toppingsPrice += parseFloat(t.value) || 0;
  });

  const total = cakePrice + designPrice + sizePrice + layersPrice + toppingsPrice;
  document.getElementById(`editTotalPrice${orderId}`).innerText = total.toFixed(2);
  document.getElementById(`editAmount${orderId}`).value = total.toFixed(2);
  return total;
}

function updateEditOrderData(orderId) {
  const cake = document.getElementById(`editCakeType${orderId}`).selectedOptions[0].text;
  const design = document.getElementById(`editDesign${orderId}`).selectedOptions[0].text;
  const size = document.getElementById(`editSize${orderId}`).selectedOptions[0].text;
  const layers = document.getElementById(`editLayers${orderId}`).selectedOptions[0].text;

  let toppings = [];
  document.querySelectorAll(`.edit-topping[data-order-id="${orderId}"]:checked`).forEach(t => {
    const label = document.querySelector(`label[for="${t.id}"]`).innerText.trim();
    toppings.push(label.split('(')[0].trim());
  });

  let description = `${cake}, ${size}, ${layers}, ${design}`;
  if (toppings.length > 0) {
    description += `, Toppings: ${toppings.join(", ")}`;
  }

  document.getElementById(`editOrderItem${orderId}`).value = description;
}

// Event listeners for edit modal changes
document.addEventListener("change", function(e) {
  if (e.target.classList.contains("edit-cake-type") ||
      e.target.classList.contains("edit-design") ||
      e.target.classList.contains("edit-size") ||
      e.target.classList.contains("edit-layers") ||
      e.target.classList.contains("edit-topping")) {
    
    const orderId = e.target.getAttribute("data-order-id");
    if (orderId) {
      calculateEditTotal(orderId);
    }
  }
});

// Initialize edit modals on page load
document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('[id^="editOrderForm"]').forEach(form => {
    const orderId = form.id.replace('editOrderForm', '');
    if (orderId) {
      calculateEditTotal(orderId);
    }
  });
});
//USER MANAGEMET SCRIPT
let currentUsersPage = 1;
const usersPerPage = 5;

function updateUsersDisplay() {
  const searchValue = document.getElementById('searchUsers').value.toLowerCase();
  const statusValue = document.getElementById('statusFilterUsers').value;
  const verifiedValue = document.getElementById('verifiedFilterUsers').value;

  const allRows = document.querySelectorAll('#usersTable tbody tr');
  let visibleRows = [];

  allRows.forEach(row => {
    const text = row.textContent.toLowerCase();
    const rowStatus = row.getAttribute('data-status');
    const rowVerified = row.getAttribute('data-verified');

    const matchSearch = text.includes(searchValue);
    const matchStatus = !statusValue || rowStatus === statusValue;
    const matchVerified = !verifiedValue || rowVerified === verifiedValue;

    if (matchSearch && matchStatus && matchVerified) visibleRows.push(row);
  });

  const totalPages = Math.ceil(visibleRows.length / usersPerPage);
  const start = (currentUsersPage - 1) * usersPerPage;
  const end = start + usersPerPage;

  allRows.forEach(row => row.style.display = 'none');
  visibleRows.forEach((row, index) => {
    if (index >= start && index < end) row.style.display = '';
  });

  updatePaginationButtons('usersPagination', currentUsersPage, totalPages, (page) => {
    currentUsersPage = page;
    updateUsersDisplay();
  });
}

document.addEventListener('DOMContentLoaded', function() {
  const searchUsers = document.getElementById('searchUsers');
  const statusFilterUsers = document.getElementById('statusFilterUsers');
  const verifiedFilterUsers = document.getElementById('verifiedFilterUsers');

  if (searchUsers) {
    searchUsers.addEventListener('keyup', () => { currentUsersPage = 1; updateUsersDisplay(); });
    statusFilterUsers.addEventListener('change', () => { currentUsersPage = 1; updateUsersDisplay(); });
    verifiedFilterUsers.addEventListener('change', () => { currentUsersPage = 1; updateUsersDisplay(); });
    updateUsersDisplay();
  }
});

// ============================================================================
// SMOOTH SCROLL
// ============================================================================
document.querySelectorAll('a[href^="#"]').forEach(anchor => {
  anchor.addEventListener('click', function (e) {
    const href = this.getAttribute('href');
    if (href !== '#') {
      e.preventDefault();
      const target = document.querySelector(href);
      if (target) {
        target.scrollIntoView({
          behavior: 'smooth',
          block: 'start'
        });
      }
    }
  });
});

// ============================================================================
// AUTO-DISMISS ALERTS
// ============================================================================
setTimeout(function() {
  const alerts = document.querySelectorAll('.alert');
  alerts.forEach(function(alert) {
    const bsAlert = new bootstrap.Alert(alert);
    bsAlert.close();
  });
}, 5000);


