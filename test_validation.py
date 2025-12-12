"""
Quick validation test script - runs validation on a CSV file directly
"""
import asyncio
import pandas as pd
from email_validator_service import validate_email_list

async def main():
    # Read the CSV
    csv_file = "email test sheet - Sheet1.csv"
    df = pd.read_csv(csv_file)
    
    print(f"Loaded {len(df)} rows from {csv_file}")
    print("\nRunning validation with SMTP checks...\n")
    
    # Run validation
    validated_df = await validate_email_list(
        df, 
        email_col='Email',
        do_smtp=True,
        mail_from='noreply@gulfconferences.co.uk',  # Use your real domain
        policy='strict'  # Treat catch-all as risky
    )
    
    # Display results
    print("\n" + "="*80)
    print("VALIDATION RESULTS")
    print("="*80)
    
    for idx, row in validated_df.iterrows():
        email = row['Email']
        normalized = row.get('normalized', 'N/A')
        bounce_risk = row['bounce_risk']
        reasons = row.get('reasons', '')
        mx_ok = row.get('mx_ok', False)
        suggestion = row.get('suggestion', '')
        catch_all = row.get('catch_all', 'unknown')
        
        status = "❌ RISKY" if bounce_risk else "✅ VALID"
        
        print(f"\n{idx+1}. {email}")
        print(f"   Status: {status}")
        print(f"   Normalized: {normalized}")
        print(f"   MX OK: {mx_ok}")
        print(f"   Catch-All: {catch_all}")
        print(f"   Reasons: {reasons if reasons else '(none)'}")
        if suggestion:
            print(f"   Suggestion: {suggestion}")
    
    # Summary
    valid_count = (~validated_df['bounce_risk']).sum()
    risky_count = validated_df['bounce_risk'].sum()
    
    print("\n" + "="*80)
    print(f"SUMMARY: {valid_count} valid, {risky_count} risky out of {len(df)} total")
    print("="*80)
    
    # Save results
    output_file = "validation_results.csv"
    validated_df.to_csv(output_file, index=False)
    print(f"\nFull results saved to: {output_file}")

if __name__ == "__main__":
    # Windows event loop fix
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
