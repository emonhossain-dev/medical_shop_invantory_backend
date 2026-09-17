# Medical SaaS - Backend

A multi-tenant SaaS platform for managing medical stores with subscription-based tiers.

## Project Structure

```
medical_saas/
├── app/
│   ├── main.py                 # FastAPI application entry point
│   ├── core/
│   │   ├── config.py           # Environment variables and settings
│   │   ├── security.py         # Password hashing and JWT handling
│   │   └── database.py         # SQLAlchemy async engine and session
│   ├── models/                 # SQLAlchemy ORM models
│   ├── schemas/                # Pydantic request/response schemas
│   ├── api/
│   │   ├── deps.py             # Shared dependencies and authentication
│   │   ├── auth/               # Authentication routes
│   │   ├── admin/              # Super admin routes
│   │   └── tenant/             # Store-scoped routes
│   ├── services/               # Business logic
│   └── repositories/           # Database query layer with store filtering
├── alembic/                    # Database migrations
├── requirements.txt            # Python dependencies
├── .env                        # Environment variables (not in version control)
├── .gitignore                  # Git ignore rules
├── alembic.ini                 # Alembic configuration
└── README.md                   # This file
```

## Features

- **Multi-Tenant Architecture**: Separate stores with automatic data filtering
- **Role-Based Access Control**: Super Admin, Store Admin, and Staff roles
- **Subscription Management**: Different tiers with feature limits
- **Medicine Management**: Add, update, and manage medicines
- **Stock Tracking**: Manage medicine inventory with expiry tracking
- **Sales Management**: Record and track medicine sales
- **Audit Logging**: Track all user actions for compliance
- **Async/Await**: Non-blocking database operations
- **JWT Authentication**: Secure token-based authentication

## Setup

### Prerequisites
- Python 3.9+
- PostgreSQL 12+
- pip

### Installation

1. Clone the repository
```bash
git clone <repository-url>
cd medical_saas
```

2. Create virtual environment
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies
```bash
pip install -r requirements.txt
```

4. Configure environment variables
```bash
cp .env.example .env
# Edit .env with your settings
```

5. Run database migrations
```bash
alembic upgrade head
```

6. Start the application
```bash
uvicorn app.main:app --reload
```

The API will be available at `http://localhost:8000`

## API Documentation

Once the application is running, visit:
- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc

## Database Models

### User
- Super Admin, Store Admin, Staff roles
- Multi-tenant support with store_id

### Store
- Medical store information
- Contact and address details
- Subscription status

### Subscription
- Plans: Free, Basic, Professional, Enterprise
- Feature limits per plan

### Medicine
- Medicine details and dosage information
- Pricing (unit price and cost price)
- Manufacturer and batch tracking

### Stock
- Inventory tracking per medicine per store
- Quantity management (in stock, reserved, available)
- Expiry date tracking

### Sale
- Sales transactions per store
- Customer information
- Payment method and status tracking

### AuditLog
- All user actions tracked
- Changes to entities (old and new values)
- IP address and user agent logging

## Key Features Implementation

### Repository Pattern
Database queries are centralized in repositories with automatic store filtering:
```python
# Ensures data is filtered by store_id automatically
medicine = await MedicineRepository.get_medicine_by_id(
    medicine_id=123,
    store_id=user.store_id,
    db=db
)
```

### Dependency Injection
Shared dependencies handle authentication and authorization:
- `get_current_user`: Authenticated user
- `get_current_store`: Current user's store
- `require_super_admin`: Super admin only
- `require_store_admin`: Admin or super admin only

### Audit Logging
All critical actions are logged:
```python
await AuditService.log_action(
    store_id=user.store_id,
    user_id=user.id,
    action="medicine_created",
    entity_type="Medicine",
    entity_id=medicine.id,
    new_values=medicine_data,
    db=db
)
```

## Security

- Passwords hashed with bcrypt
- JWT tokens for stateless authentication
- SQL injection prevention with SQLAlchemy ORM
- CORS enabled for frontend
- Role-based access control on all endpoints

## Development

### Run tests
```bash
pytest
```

### Lint code
```bash
flake8 app
```

### Format code
```bash
black app
```

## License

MIT License
