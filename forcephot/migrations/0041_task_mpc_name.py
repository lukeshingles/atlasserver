# Generated by Django 3.1.7 on 2021-02-23 14:10

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('forcephot', '0040_delete_result'),
    ]

    operations = [
        migrations.AddField(
            model_name='task',
            name='mpc_name',
            field=models.CharField(blank=True, default=None, max_length=300, null=True, verbose_name='Minor Planet Center object name (overrides RA/Dec)'),
        ),
    ]