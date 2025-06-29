# GitHub Actions Setup for Redfin Scraper

This guide will help you set up automated Redfin scraping that runs every 12 hours using GitHub Actions (free tier).

## 📋 Prerequisites

1. A public GitHub repository with this code
2. An email account for sending results (Outlook/Hotmail recommended for easiest setup)

## 🔐 Required GitHub Secrets

You need to set these secrets in your GitHub repository:

### Go to: `Repository → Settings → Secrets and variables → Actions → New repository secret`

**Required Secrets:**
- `EMAIL_ADDRESS` - Your email address (e.g., `your-email@outlook.com`)
- `EMAIL_PASSWORD` - Your email password (see provider-specific instructions below)

## 📧 Email Provider Setup

### Option 1: Outlook/Hotmail (Recommended - Easiest)
✅ **Most reliable for GitHub Actions**
- Use any `@outlook.com`, `@hotmail.com`, or `@live.com` email
- Secret values:
  - `EMAIL_ADDRESS`: `your-email@outlook.com`
  - `EMAIL_PASSWORD`: `your-regular-password`

### Option 2: Gmail (Requires App Password)
⚠️ **More complex setup**
- You must enable 2-factor authentication
- Generate an App Password (not your regular password)
- Secret values:
  - `EMAIL_ADDRESS`: `your-email@gmail.com`  
  - `EMAIL_PASSWORD`: `your-16-character-app-password`

### Option 3: Other Providers
- Yahoo, AOL also supported
- May require app passwords depending on provider

## ⚙️ GitHub Actions Configuration

The workflow is configured to:
- ✅ Run every 12 hours (6 AM & 6 PM UTC)
- ✅ Use ~5-10 minutes per run (well within free tier)
- ✅ Auto-email results to `jessejames1125@gmail.com`
- ✅ Upload debug artifacts if the job fails
- ✅ 30-minute timeout to prevent runaway jobs

### Free Tier Usage:
- **2 runs/day × 10 min/run × 30 days = ~600 minutes/month**
- **GitHub Free: 2000 minutes/month** ✅ Safe margin

## 🚀 Activation Steps

1. **Set the secrets** (see above)
2. **Push this code** to your GitHub repository
3. **Enable Actions**: Go to `Repository → Actions` and enable workflows
4. **Test manually**: Click `Actions → Redfin Spokane Real Estate Scraper → Run workflow`

## 📊 Monitoring

### Check if it's working:
- Go to `Repository → Actions` to see run history
- Green checkmark = success, emails sent
- Red X = failure, check logs

### Email delivery:
- Results emailed to: `jessejames1125@gmail.com`
- Contains: Excel file + PDF report + summary

## 🛠 Troubleshooting

### Common Issues:

**1. "Email credentials not found"**
- Check that `EMAIL_ADDRESS` and `EMAIL_PASSWORD` secrets are set correctly
- Verify no extra spaces in secret values

**2. "Authentication failed"** 
- Gmail: Make sure you're using an App Password, not regular password
- Outlook: Try enabling "Less secure app access" if needed

**3. "Workflow not running"**
- Check that GitHub Actions are enabled for your repository
- Verify the workflow file is in `.github/workflows/` directory

**4. "Job timeout"**
- The 30-minute limit should be plenty, but if sites are slow, you can increase timeout

### Manual Testing:
```bash
# Test locally first:
python redfin_scraper.py --test-email  # Safe preview mode
python redfin_scraper.py --send-email --provider outlook  # Real email
```

## 🔒 Security Notes

- ✅ Secrets are encrypted in GitHub and not visible in logs
- ✅ Repository can be public; secrets remain private
- ✅ Email credentials never appear in code or logs
- ✅ Files are created temporarily and deleted after each run

## 📈 Customization

### Change email recipient:
Edit `EMAIL_RECIPIENT` in `redfin_scraper.py`:
```python
EMAIL_RECIPIENT = "your-email@example.com"
```

### Change schedule:
Edit `.github/workflows/redfin-scraper.yml`:
```yaml
schedule:
  - cron: '0 8,20 * * *'  # 8 AM and 8 PM UTC
```

### Add property limit:
```yaml
run: |
  python redfin_scraper.py --send-email --provider outlook --limit 50
``` 